from flask import Flask, jsonify, request, session, g
from flask_sock import Sock
from simple_websocket import ConnectionClosed   # flask-sock 설치 시 함께 설치됨
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
import json
import os
import re
import secrets
import sqlite3
import threading

app = Flask(__name__)
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
# 25초마다 ping을 보내 대화가 없어도 연결이 끊기지 않게 유지
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'taxi.db'          # 이전 notice.db와 분리해서 새로 시작

KST = timezone(timedelta(hours=9))  # 컨테이너 기본 시간이 UTC라 한국 시간을 직접 지정
CAPACITY = 4                        # 파티 정원 고정


def now_kst():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


# ---------- 세션 서명 키 ----------
# compose에 환경변수를 추가하지 않도록, 처음 실행 때 랜덤 키를 만들어 볼륨(/data)에 저장.
# 재시작해도 같은 키를 읽으므로 로그인이 풀리지 않음.
KEY_FILE = DATA_DIR / 'secret_key'
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32))
app.secret_key = KEY_FILE.read_text().strip()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # JS에서 쿠키를 읽지 못하게
    SESSION_COOKIE_SAMESITE='Lax',    # 다른 사이트에서 보낸 요청에 쿠키가 실리는 것 제한
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


# ---------- DB ----------
def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')   # SQLite는 외래키 검사가 기본으로 꺼져 있음
    return db


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    student_id    TEXT    NOT NULL UNIQUE,
    department    TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS party (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meet_place    TEXT    NOT NULL,
    destination   TEXT    NOT NULL,
    depart_at     TEXT    NOT NULL,                 -- 'YYYY-MM-DD HH:MM' (한국 시간)
    member_count  INTEGER NOT NULL DEFAULT 0 CHECK (member_count BETWEEN 0 AND 4),
    host_user_id  INTEGER NOT NULL REFERENCES users(id),   -- 작성자 = 방장, 바뀌지 않음
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS party_member (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id   INTEGER NOT NULL REFERENCES party(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    joined_at  TEXT    NOT NULL,
    UNIQUE (party_id, user_id)
);

CREATE TABLE IF NOT EXISTS party_kick (
    party_id   INTEGER NOT NULL REFERENCES party(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (party_id, user_id)
);

CREATE TABLE IF NOT EXISTS message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id   INTEGER NOT NULL REFERENCES party(id),
    user_id    INTEGER REFERENCES users(id),       -- 시스템 메시지는 NULL
    content    TEXT    NOT NULL,
    type       TEXT    NOT NULL DEFAULT 'chat' CHECK (type IN ('chat', 'system')),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_party_depart  ON party(depart_at);
CREATE INDEX IF NOT EXISTS idx_message_party ON message(party_id, id);
'''

with closing(connect()) as db:
    db.execute('PRAGMA journal_mode = WAL')   # 읽기와 쓰기가 동시에 일어나도 덜 막히게
    db.executescript(SCHEMA)


# ---------- 공통 함수 ----------
def error(message, status):
    return jsonify(error=message), status


def text_field(data, key, max_len):
    """문자열 필드를 꺼내 앞뒤 공백 제거 후 반환. 비었거나 너무 길면 None."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        return None
    return value.strip()


def mask_name(name):
    """다른 사용자에게 보여줄 이름: 신정민 → 신*민, 정민 → 정*"""
    if len(name) <= 2:
        return name[0] + '*'
    return name[0] + '*' * (len(name) - 2) + name[-1]


def public_user(row):
    """다른 사용자에게 보여줄 정보 (학번·아이디 제외)"""
    return {'id': row['id'], 'name': mask_name(row['name']), 'department': row['department']}


def login_required(view):
    """로그인한 사용자만 통과. 통과하면 g.user에 사용자 정보가 들어감."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        user_id = session.get('user_id')
        if user_id is None:
            return error('로그인이 필요합니다.', 401)
        with closing(connect()) as db:
            user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if user is None:            # DB가 초기화되는 등 계정이 사라진 경우
            session.clear()
            return error('로그인이 필요합니다.', 401)
        g.user = user
        return view(*args, **kwargs)
    return wrapper


@app.errorhandler(413)
def too_large(e):
    return error('입력 내용이 너무 큽니다.', 413)


@app.get('/api/health')
def health():
    return jsonify(status='ok', student_id='2224037', name='신정민')


# ---------- 인증 ----------
USERNAME_RE = re.compile(r'[a-z0-9]{4,20}')
STUDENT_ID_RE = re.compile(r'\d{7,10}')     # 학교 학번 자릿수에 맞게 조정

# 없는 아이디로 로그인해도 비밀번호 검사 시간을 똑같이 써서,
# 응답 시간 차이로 "존재하는 아이디"를 알아내지 못하게 함
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


# 회원가입
@app.post('/api/users')
def signup():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)

    username = data.get('username')
    password = data.get('password')
    student_id = data.get('student_id')
    department = text_field(data, 'department', 30)
    name = text_field(data, 'name', 20)

    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        return error('아이디는 영문 소문자·숫자 4~20자로 입력하세요.', 400)
    if not isinstance(password, str) or not 8 <= len(password) <= 64:
        return error('비밀번호는 8~64자로 입력하세요.', 400)
    if not isinstance(student_id, str) or not STUDENT_ID_RE.fullmatch(student_id):
        return error('학번은 숫자로 입력하세요.', 400)
    if department is None:
        return error('학과를 입력하세요.', 400)
    if name is None or len(name) < 2:
        return error('이름은 2~20자로 입력하세요.', 400)

    try:
        with closing(connect()) as db, db:
            if db.execute('SELECT 1 FROM users WHERE username = ?', (username,)).fetchone():
                return error('이미 사용 중인 아이디입니다.', 409)
            if db.execute('SELECT 1 FROM users WHERE student_id = ?', (student_id,)).fetchone():
                return error('이미 가입된 학번입니다.', 409)
            cur = db.execute(
                'INSERT INTO users (username, password_hash, student_id, department, name, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (username, generate_password_hash(password), student_id, department, name, now_kst()))
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # 위 검사와 INSERT 사이에 같은 아이디·학번 가입이 끼어든 경우 UNIQUE 제약이 막음
        return error('이미 사용 중인 아이디 또는 학번입니다.', 409)

    return jsonify(id=user_id, username=username, name=name, department=department), 201


# 로그인
@app.post('/api/sessions')
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)
    username = data.get('username')
    password = data.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return error('아이디와 비밀번호를 입력하세요.', 400)

    with closing(connect()) as db:
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

    password_hash = user['password_hash'] if user else DUMMY_HASH
    if not check_password_hash(password_hash, password) or user is None:
        # 아이디가 틀렸는지 비밀번호가 틀렸는지 구분해서 알려주지 않음
        return error('아이디 또는 비밀번호가 올바르지 않습니다.', 401)

    session.clear()                 # 이전 세션 정보를 지우고 새로 발급
    session['user_id'] = user['id']
    session.permanent = True
    return jsonify(id=user['id'], username=user['username'],
                   name=user['name'], department=user['department'])


# 로그아웃
@app.delete('/api/sessions/current')
def logout():
    session.clear()
    return '', 204


# 내 정보 (본인에게는 이름을 가리지 않고 보여줌)
@app.get('/api/users/me')
@login_required
def me():
    u = g.user
    return jsonify(id=u['id'], username=u['username'], student_id=u['student_id'],
                   department=u['department'], name=u['name'])


# ==========================================================
# 택시 파티
# ==========================================================

# ---------- 실시간 연결 관리 (WebSocket) ----------
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


# ---------- 파티 공통 함수 ----------
MAX_DAYS_AHEAD = 7    # 출발 시간은 지금부터 7일 이내만 허용 (장난성 글 방지)


def now_minute():
    """현재 한국 시간을 depart_at과 같은 형식('YYYY-MM-DD HH:MM')으로"""
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M')


def parse_depart(value):
    """출발 시간 검사. 'YYYY-MM-DD HH:MM' 또는 <input type="datetime-local">의
    'YYYY-MM-DDTHH:MM'을 받아 'YYYY-MM-DD HH:MM'으로 반환. 잘못되면 (None, 에러메시지)."""
    if not isinstance(value, str):
        return None, '출발 시간을 입력하세요.'
    try:
        depart = datetime.strptime(value.strip().replace('T', ' '), '%Y-%m-%d %H:%M')
    except ValueError:
        return None, '출발 시간 형식이 올바르지 않습니다. (예: 2026-11-20 18:30)'
    depart = depart.replace(tzinfo=KST)
    now = datetime.now(KST)
    if depart <= now:
        return None, '출발 시간은 현재 이후여야 합니다.'
    if depart > now + timedelta(days=MAX_DAYS_AHEAD):
        return None, f'출발 시간은 {MAX_DAYS_AHEAD}일 이내로 정해주세요.'
    return depart.strftime('%Y-%m-%d %H:%M'), None


def read_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def display_name(user):
    """시스템 메시지용 표시 이름: '컴퓨터공학과 신*민'"""
    return f"{user['department']} {mask_name(user['name'])}"


PARTY_SELECT = '''
    SELECT p.*, u.name AS host_name, u.department AS host_department,
           EXISTS (SELECT 1 FROM party_member m
                   WHERE m.party_id = p.id AND m.user_id = :me) AS joined
    FROM party p JOIN users u ON u.id = p.host_user_id
'''


def party_json(row, me_id):
    """파티 행을 응답용 dict로. 마감 여부는 저장하지 않고 여기서 계산."""
    is_full = row['member_count'] >= CAPACITY
    is_expired = row['depart_at'] < now_minute()
    return {
        'id': row['id'],
        'meet_place': row['meet_place'],
        'destination': row['destination'],
        'depart_at': row['depart_at'],
        'member_count': row['member_count'],
        'capacity': CAPACITY,
        'host': {'id': row['host_user_id'],
                 'name': mask_name(row['host_name']),
                 'department': row['host_department']},
        'is_full': is_full,
        'is_expired': is_expired,
        'is_closed': is_full or is_expired,
        'is_host': row['host_user_id'] == me_id,
        'joined': bool(row['joined']),
        'created_at': row['created_at'],
    }


def fetch_party(db, party_id, me_id):
    return db.execute(PARTY_SELECT + ' WHERE p.id = :id', {'me': me_id, 'id': party_id}).fetchone()


def fetch_members(db, party_id, host_id):
    rows = db.execute('''SELECT u.id, u.name, u.department, m.joined_at
                         FROM party_member m JOIN users u ON u.id = m.user_id
                         WHERE m.party_id = ? ORDER BY m.id''', (party_id,)).fetchall()
    return [{**public_user(r), 'is_host': r['id'] == host_id, 'joined_at': r['joined_at']} for r in rows]


def is_member(db, party_id, user_id):
    return db.execute('SELECT 1 FROM party_member WHERE party_id = ? AND user_id = ?',
                      (party_id, user_id)).fetchone() is not None


def add_system_message(db, party_id, content):
    """시스템 메시지를 저장하고 전송용 dict 반환 (트랜잭션 안에서 호출)"""
    created_at = now_kst()
    cur = db.execute("INSERT INTO message (party_id, user_id, content, type, created_at) "
                     "VALUES (?, NULL, ?, 'system', ?)", (party_id, content, created_at))
    return {'id': cur.lastrowid, 'type': 'system', 'content': content, 'created_at': created_at}


def notify_change(party_id, system_msg=None):
    """입장·나가기·강퇴·수정 후 공통 알림: 채팅방에 시스템 메시지 + 참여자 목록, 목록 화면에 파티 정보.
    DB 트랜잭션이 끝난(커밋된) 뒤에 호출해야 함."""
    with closing(connect()) as db:
        row = fetch_party(db, party_id, None)
        if row is None:
            return
        members = fetch_members(db, party_id, row['host_user_id'])
    party = party_json(row, None)
    # 받는 사람마다 다른 값(is_host, joined)은 빼고 보냄
    for key in ('is_host', 'joined'):
        party.pop(key)
    if system_msg:
        broadcast_room(party_id, {'type': 'system', 'message': system_msg})
    broadcast_room(party_id, {'type': 'members', 'member_count': row['member_count'],
                              'host_user_id': row['host_user_id'], 'members': members})
    broadcast_room(party_id, {'type': 'party_updated', 'party': party})
    broadcast_lobby({'type': 'party_update', 'party': party})


# ---------- 파티 API ----------

# 목록·검색: 출발 시간이 지나지 않은 파티를 출발 시간 순으로
@app.get('/api/parties')
@login_required
def list_parties():
    me_id = g.user['id']
    sql = PARTY_SELECT + ' WHERE p.depart_at >= :now'
    params = {'me': me_id, 'now': now_minute()}

    destination = request.args.get('destination', '').strip()
    if destination:
        # LIKE의 특수문자(%, _)를 그대로 검색하도록 처리
        escaped = destination.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        sql += " AND p.destination LIKE :dest ESCAPE '\\'"
        params['dest'] = f'%{escaped}%'

    date = request.args.get('date', '').strip()
    if date:
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            return error('날짜 형식이 올바르지 않습니다. (예: 2026-11-20)', 400)
        sql += ' AND p.depart_at LIKE :date'
        params['date'] = f'{date}%'

    sql += ' ORDER BY p.depart_at, p.id'
    with closing(connect()) as db:
        rows = db.execute(sql, params).fetchall()
    return jsonify(parties=[party_json(r, me_id) for r in rows])


# 파티 생성: 작성자가 방장이 되고 자동으로 첫 참여자가 됨
@app.post('/api/parties')
@login_required
def create_party():
    data = read_json()
    if data is None:
        return error('JSON 형식으로 보내주세요.', 400)
    meet_place = text_field(data, 'meet_place', 50)
    destination = text_field(data, 'destination', 50)
    if meet_place is None or destination is None:
        return error('만나는 장소와 목적지를 입력하세요. (각 50자 이내)', 400)
    depart_at, err = parse_depart(data.get('depart_at'))
    if err:
        return error(err, 400)

    me_id = g.user['id']
    now = now_kst()
    with closing(connect()) as db, db:
        cur = db.execute('INSERT INTO party (meet_place, destination, depart_at, member_count, '
                         'host_user_id, created_at) VALUES (?, ?, ?, 1, ?, ?)',
                         (meet_place, destination, depart_at, me_id, now))
        party_id = cur.lastrowid
        db.execute('INSERT INTO party_member (party_id, user_id, joined_at) VALUES (?, ?, ?)',
                   (party_id, me_id, now))
        add_system_message(db, party_id, f'{display_name(g.user)}님이 파티를 만들었습니다.')
        row = fetch_party(db, party_id, me_id)

    notify_change(party_id)
    return jsonify(party_json(row, me_id)), 201


# 파티 상세: 참여자만 볼 수 있음
@app.get('/api/parties/<int:party_id>')
@login_required
def get_party(party_id):
    me_id = g.user['id']
    with closing(connect()) as db:
        row = fetch_party(db, party_id, me_id)
        if row is None:
            return error('존재하지 않는 파티입니다.', 404)
        if not row['joined']:
            return error('파티 참여자만 볼 수 있습니다.', 403)
        members = fetch_members(db, party_id, row['host_user_id'])
    return jsonify(party=party_json(row, me_id), members=members)


FIELD_LABELS = {'meet_place': '만나는 장소를', 'destination': '목적지를', 'depart_at': '출발 시간을'}


# 파티 수정 (방장): 보낸 항목만 바꿈
@app.patch('/api/parties/<int:party_id>')
@login_required
def update_party(party_id):
    data = read_json()
    if data is None:
        return error('JSON 형식으로 보내주세요.', 400)

    changes = {}
    if 'meet_place' in data:
        changes['meet_place'] = text_field(data, 'meet_place', 50)
    if 'destination' in data:
        changes['destination'] = text_field(data, 'destination', 50)
    if None in changes.values():
        return error('만나는 장소와 목적지는 50자 이내로 입력하세요.', 400)
    if 'depart_at' in data:
        depart_at, err = parse_depart(data['depart_at'])
        if err:
            return error(err, 400)
        changes['depart_at'] = depart_at
    if not changes:
        return error('수정할 항목(meet_place, destination, depart_at)을 보내주세요.', 400)

    me_id = g.user['id']
    with closing(connect()) as db, db:
        old = db.execute('SELECT * FROM party WHERE id = ?', (party_id,)).fetchone()
        if old is None:
            return error('존재하지 않는 파티입니다.', 404)
        if old['host_user_id'] != me_id:
            return error('방장만 수정할 수 있습니다.', 403)
        if old['depart_at'] < now_minute():
            return error('출발 시간이 지난 파티는 수정할 수 없습니다.', 409)

        changed = {k: v for k, v in changes.items() if v != old[k]}
        system_msg = None
        if changed:
            # 컬럼 이름은 위에서 정한 세 개만 들어가므로 SQL에 직접 넣어도 안전
            sets = ', '.join(f'{k} = ?' for k in changed)
            db.execute(f'UPDATE party SET {sets} WHERE id = ?', (*changed.values(), party_id))
            detail = ', '.join(f'{FIELD_LABELS[k]} {old[k]} → {v}' for k, v in changed.items())
            system_msg = add_system_message(db, party_id, f'방장이 {detail}(으)로 변경했습니다.')
        row = fetch_party(db, party_id, me_id)

    if changed:
        notify_change(party_id, system_msg)
    return jsonify(party_json(row, me_id))


# 파티 삭제 (방장): 채팅방(참여자·강퇴 기록·메시지)도 함께 삭제
@app.delete('/api/parties/<int:party_id>')
@login_required
def delete_party(party_id):
    me_id = g.user['id']
    with closing(connect()) as db, db:
        party = db.execute('SELECT host_user_id FROM party WHERE id = ?', (party_id,)).fetchone()
        if party is None:
            return error('존재하지 않는 파티입니다.', 404)
        if party['host_user_id'] != me_id:
            return error('방장만 삭제할 수 있습니다.', 403)
        # 외래키가 켜져 있으므로 참조하는 쪽부터 지움
        db.execute('DELETE FROM message WHERE party_id = ?', (party_id,))
        db.execute('DELETE FROM party_kick WHERE party_id = ?', (party_id,))
        db.execute('DELETE FROM party_member WHERE party_id = ?', (party_id,))
        db.execute('DELETE FROM party WHERE id = ?', (party_id,))

    close_room(party_id, {'type': 'party_deleted'})
    broadcast_lobby({'type': 'party_removed', 'party_id': party_id})
    return '', 204


# 입장: 정원 확인과 인원 증가를 한 문장으로 처리 → 동시에 눌러도 4명 초과 불가
@app.post('/api/parties/<int:party_id>/members')
@login_required
def join_party(party_id):
    me_id = g.user['id']
    try:
        with closing(connect()) as db, db:
            party = db.execute('SELECT depart_at FROM party WHERE id = ?', (party_id,)).fetchone()
            if party is None:
                return error('존재하지 않는 파티입니다.', 404)
            if db.execute('SELECT 1 FROM party_kick WHERE party_id = ? AND user_id = ?',
                          (party_id, me_id)).fetchone():
                return error('강퇴된 파티에는 다시 입장할 수 없습니다.', 403)
            if is_member(db, party_id, me_id):
                return error('이미 참여 중인 파티입니다.', 409)

            now = now_minute()
            if party['depart_at'] < now:
                return error('출발 시간이 지난 파티입니다.', 409)

            cur = db.execute('UPDATE party SET member_count = member_count + 1 '
                             'WHERE id = ? AND member_count < ? AND depart_at >= ?',
                             (party_id, CAPACITY, now))
            if cur.rowcount == 0:
                return error('정원(4명)이 가득 찼습니다.', 409)

            db.execute('INSERT INTO party_member (party_id, user_id, joined_at) VALUES (?, ?, ?)',
                       (party_id, me_id, now_kst()))
            system_msg = add_system_message(db, party_id, f'{display_name(g.user)}님이 입장했습니다.')
            row = fetch_party(db, party_id, me_id)
    except sqlite3.IntegrityError:
        # 같은 사람이 입장 버튼을 동시에 두 번 누른 경우: UNIQUE가 막고 인원 증가도 롤백됨
        return error('이미 참여 중인 파티입니다.', 409)

    notify_change(party_id, system_msg)
    return jsonify(party_json(row, me_id)), 201


# 나가기(me) / 강퇴(다른 사용자 id): "참여자 삭제"를 한 함수에서 처리
@app.delete('/api/parties/<int:party_id>/members/<target>')
@login_required
def remove_member(party_id, target):
    me_id = g.user['id']
    if target == 'me':
        target_id = me_id
    elif target.isdigit():
        target_id = int(target)
    else:
        return error('잘못된 사용자 번호입니다.', 400)
    leaving = target_id == me_id

    with closing(connect()) as db, db:
        party = db.execute('SELECT host_user_id FROM party WHERE id = ?', (party_id,)).fetchone()
        if party is None:
            return error('존재하지 않는 파티입니다.', 404)
        is_host = party['host_user_id'] == me_id

        if leaving and is_host:
            return error('방장은 나갈 수 없습니다. 모집글을 삭제하세요.', 409)
        if not leaving and not is_host:
            return error('방장만 강퇴할 수 있습니다.', 403)

        target_user = db.execute('SELECT * FROM users WHERE id = ?', (target_id,)).fetchone()
        cur = db.execute('DELETE FROM party_member WHERE party_id = ? AND user_id = ?',
                         (party_id, target_id))
        if cur.rowcount == 0:
            return error('파티에 참여 중이 아닙니다.' if leaving else '해당 참여자가 없습니다.', 404)
        db.execute('UPDATE party SET member_count = member_count - 1 WHERE id = ?', (party_id,))

        if leaving:
            text = f'{display_name(target_user)}님이 나갔습니다.'
        else:
            db.execute('INSERT OR IGNORE INTO party_kick (party_id, user_id) VALUES (?, ?)',
                       (party_id, target_id))
            text = f'{display_name(target_user)}님이 강퇴되었습니다.'
        system_msg = add_system_message(db, party_id, text)

    if not leaving:
        close_room_member(party_id, target_id, {'type': 'kicked'})
    notify_change(party_id, system_msg)
    return '', 204


# 채팅 기록: 최근 50개, ?before_id=로 그 이전 기록 더 보기
MESSAGE_PAGE = 50


def message_json(row):
    msg = {'id': row['id'], 'type': row['type'], 'content': row['content'],
           'created_at': row['created_at']}
    if row['type'] == 'chat':
        msg['user'] = {'id': row['user_id'], 'name': mask_name(row['name']),
                       'department': row['department']}
    return msg


@app.get('/api/parties/<int:party_id>/messages')
@login_required
def list_messages(party_id):
    before_id = request.args.get('before_id', type=int)
    with closing(connect()) as db:
        if db.execute('SELECT 1 FROM party WHERE id = ?', (party_id,)).fetchone() is None:
            return error('존재하지 않는 파티입니다.', 404)
        if not is_member(db, party_id, g.user['id']):
            return error('파티 참여자만 볼 수 있습니다.', 403)
        sql = '''SELECT m.*, u.name, u.department FROM message m
                 LEFT JOIN users u ON u.id = m.user_id WHERE m.party_id = ?'''
        params = [party_id]
        if before_id:
            sql += ' AND m.id < ?'
            params.append(before_id)
        sql += ' ORDER BY m.id DESC LIMIT ?'
        params.append(MESSAGE_PAGE)
        rows = db.execute(sql, params).fetchall()
    rows.reverse()   # 화면에는 오래된 것부터
    return jsonify(messages=[message_json(r) for r in rows],
                   has_more=len(rows) == MESSAGE_PAGE)


# ==========================================================
# WebSocket
# ==========================================================
# REST로 상태가 바뀌면 위의 broadcast_* 함수가 이 연결들로 알림을 보냄.
# 클라이언트가 WebSocket으로 직접 보내는 것은 채팅 메시지뿐.

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
        app.logger.exception('채팅방 연결 처리 중 오류')
    finally:
        with registry_lock:
            conns = room_conns.get(party_id)
            if conns is not None:
                conns.discard(conn)
                if not conns:
                    room_conns.pop(party_id, None)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
