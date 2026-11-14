# 실시간 연결 관리 (WebSocket)
# REST로 상태가 바뀌면 broadcast_* 함수가 이 연결들로 알림을 보냄.
# 클라이언트가 WebSocket으로 직접 보내는 것은 채팅 메시지뿐.
from flask import current_app, session
from flask_sock import Sock
from simple_websocket import ConnectionClosed   # flask-sock 설치 시 함께 설치됨
from contextlib import closing
import json
import threading

from database import connect, now_kst, is_member
from common import public_user

sock = Sock()   # app.py에서 sock.init_app(app)으로 연결


# 접속 중인 연결을 서버 메모리에 보관. 서버가 1대일 때만 맞는 방식이라,
# 서버를 여러 대로 늘리면 Redis Pub/Sub 같은 공유 채널이 필요함 (README 참고).
class Conn:
    """WebSocket 연결 하나. 여러 스레드가 동시에 send하지 않도록 연결마다 잠금을 둠."""

    def __init__(self, ws, user_id):
        self.ws = ws
        self.user_id = user_id
        self.lock = threading.Lock()

    def send(self, event):
        try:
            with self.lock:
                self.ws.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass   # 이미 끊긴 연결이면 무시 (등록 해제는 연결 쪽 finally에서 처리)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


registry_lock = threading.Lock()
lobby_conns = set()     # 목록 화면 연결들
room_conns = {}         # party_id → 채팅방 연결들(set)


def broadcast_room(party_id, event):
    """채팅방에 접속 중인 사람들에게 event(dict) 전송"""
    with registry_lock:
        conns = list(room_conns.get(party_id, ()))
    for conn in conns:
        conn.send(event)


def broadcast_lobby(event):
    """목록 화면에 접속 중인 사람들에게 event(dict) 전송"""
    with registry_lock:
        conns = list(lobby_conns)
    for conn in conns:
        conn.send(event)


def close_room_member(party_id, user_id, event):
    """특정 사용자의 채팅방 연결에 event를 보내고 끊기 (강퇴용, 탭 여러 개면 모두)"""
    with registry_lock:
        conns = [c for c in room_conns.get(party_id, ()) if c.user_id == user_id]
    for conn in conns:
        conn.send(event)
        conn.close()


def close_room(party_id, event):
    """채팅방 전체에 event를 보내고 모두 끊기 (모집글 삭제용)"""
    with registry_lock:
        conns = list(room_conns.pop(party_id, ()))
    for conn in conns:
        conn.send(event)
        conn.close()


def ws_user():
    """WebSocket 연결 요청의 로그인 쿠키로 사용자 확인 (없으면 None)"""
    user_id = session.get('user_id')
    if user_id is None:
        return None
    with closing(connect()) as db:
        return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


def ws_reject(ws, message):
    ws.send(json.dumps({'type': 'error', 'error': message}, ensure_ascii=False))
    ws.close()


# 목록 화면: 파티 생성·수정·삭제·인원 변화 알림만 받음
@sock.route('/api/ws/lobby')
def ws_lobby(ws):
    user = ws_user()
    if user is None:
        return ws_reject(ws, '로그인이 필요합니다.')

    conn = Conn(ws, user['id'])
    with registry_lock:
        lobby_conns.add(conn)
    try:
        while True:
            ws.receive()    # 보내는 내용은 없음. 연결이 끊길 때까지 대기
    except ConnectionClosed:
        pass
    finally:
        with registry_lock:
            lobby_conns.discard(conn)


CHAT_MAX = 500


# 채팅방: 참여자만 연결 가능, 채팅 메시지 송수신
@sock.route('/api/ws/parties/<int:party_id>')
def ws_party(ws, party_id):
    user = ws_user()
    if user is None:
        return ws_reject(ws, '로그인이 필요합니다.')

    conn = Conn(ws, user['id'])
    # 먼저 등록하고 나서 참여 여부를 확인해야, 그 사이에 강퇴돼도 알림을 놓치지 않음
    with registry_lock:
        room_conns.setdefault(party_id, set()).add(conn)
    try:
        with closing(connect()) as db:
            if not is_member(db, party_id, user['id']):
                return ws_reject(ws, '파티 참여자만 입장할 수 있습니다.')

        while True:
            raw = ws.receive()
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                conn.send({'type': 'error', 'error': '잘못된 형식입니다.'})
                continue
            if not isinstance(data, dict) or data.get('type') != 'chat':
                conn.send({'type': 'error', 'error': '알 수 없는 요청입니다.'})
                continue

            content = data.get('content')
            if not isinstance(content, str) or not content.strip() or len(content) > CHAT_MAX:
                conn.send({'type': 'error', 'error': f'메시지는 1~{CHAT_MAX}자로 입력하세요.'})
                continue
            content = content.strip()

            with closing(connect()) as db, db:
                # 강퇴·삭제된 직후 보낸 메시지를 막기 위해 매번 다시 확인
                if not is_member(db, party_id, user['id']):
                    return ws_reject(ws, '파티 참여자가 아닙니다.')
                created_at = now_kst()
                cur = db.execute("INSERT INTO message (party_id, user_id, content, type, created_at) "
                                 "VALUES (?, ?, ?, 'chat', ?)", (party_id, user['id'], content, created_at))
                message = {'id': cur.lastrowid, 'type': 'chat', 'content': content,
                           'created_at': created_at, 'user': public_user(user)}
            broadcast_room(party_id, {'type': 'chat', 'message': message})
    except ConnectionClosed:
        pass    # 사용자가 나가거나 강퇴·삭제로 연결이 끊김
    except Exception:
        current_app.logger.exception('채팅방 연결 처리 중 오류')
    finally:
        with registry_lock:
            conns = room_conns.get(party_id)
            if conns is not None:
                conns.discard(conn)
                if not conns:
                    room_conns.pop(party_id, None)
