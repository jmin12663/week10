"""WebSocket 실시간 동작 시험. 사용법: python3 test_ws.py http://127.0.0.1:5000"""
import json, sys, time, urllib.request, urllib.parse, http.cookiejar
from datetime import datetime, timedelta, timezone
import simple_websocket

import random
RUN = random.randint(100, 999)
B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5000'
WS = B.replace('http', 'ws', 1)
KST = timezone(timedelta(hours=9))
fails = 0


class Client:
    def __init__(self, n):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.req('POST', '/api/users', {'username': f'ws{RUN}u{n}', 'password': 'password1',
                                        'student_id': f'8{RUN}{n:03d}', 'department': '컴퓨터공학과',
                                        'name': f'이영희{n}'})
        self.req('POST', '/api/sessions', {'username': f'ws{RUN}u{n}', 'password': 'password1'})
        self.id = self.req('GET', '/api/users/me')[1]['id']

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(B + path, data=data, method=method, headers={'Content-Type': 'application/json'})
        try:
            with self.op.open(r) as res:
                t = res.read().decode(); return res.status, (json.loads(t) if t else None)
        except urllib.error.HTTPError as e:
            t = e.read().decode(); return e.code, (json.loads(t) if t else None)

    def ws(self, path):
        cookie = '; '.join(f'{c.name}={c.value}' for c in self.jar)
        return simple_websocket.Client.connect(WS + path, headers={'Cookie': cookie})


def recv_until(ws, type_, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = ws.receive(timeout=max(0.05, end - time.time()))
        except simple_websocket.ConnectionClosed:
            return 'CLOSED'
        if raw is None:
            continue
        ev = json.loads(raw)
        if ev['type'] == type_:
            return ev
    return None


def check(label, ok, detail=''):
    global fails
    fails += 0 if ok else 1
    print(f"{'✅' if ok else '❌'} {label} {detail}")


a, b, c = Client(1), Client(2), Client(3)
later = (datetime.now(KST) + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')

# 비로그인 연결 거부
anon = simple_websocket.Client.connect(WS + '/api/ws/lobby')
ev = recv_until(anon, 'error')
check('비로그인 lobby 연결 거부', isinstance(ev, dict), ev and ev.get('error'))

lobby = c.ws('/api/ws/lobby')
time.sleep(0.2)
pid = a.req('POST', '/api/parties', {'meet_place': '정문', 'destination': '양주역', 'depart_at': later})[1]['id']
ev = recv_until(lobby, 'party_update')
check('생성 → 목록에 실시간 알림', ev not in (None, 'CLOSED') and ev['party']['id'] == pid)

# 비참여자 채팅방 연결 거부
ws_c = c.ws(f'/api/ws/parties/{pid}')
ev = recv_until(ws_c, 'error')
check('비참여자 채팅방 연결 거부', isinstance(ev, dict), ev and ev.get('error'))

ws_a = a.ws(f'/api/ws/parties/{pid}')
time.sleep(0.2)
b.req('POST', f'/api/parties/{pid}/members')
ev = recv_until(ws_a, 'system')
check('B 입장 → A 채팅방에 시스템 메시지', isinstance(ev, dict), ev and ev['message']['content'])
ev = recv_until(ws_a, 'members')
check('B 입장 → A 화면 인원 갱신', isinstance(ev, dict) and ev['member_count'] == 2, ev and f"{ev['member_count']}/4")
ev = recv_until(lobby, 'party_update')
check('B 입장 → 목록 인원 갱신', isinstance(ev, dict) and ev['party']['member_count'] == 2)

ws_b = b.ws(f'/api/ws/parties/{pid}')
time.sleep(0.2)
ws_a.send(json.dumps({'type': 'chat', 'content': '정문 편의점 앞에서 봬요'}))
ev = recv_until(ws_b, 'chat')
check('A 메시지 → B 수신', isinstance(ev, dict), ev and f"{ev['message']['user']['name']}: {ev['message']['content']}")

ws_a.send(json.dumps({'type': 'chat', 'content': '   '}))
ev = recv_until(ws_a, 'error')
check('빈 메시지 거부', isinstance(ev, dict), ev and ev.get('error'))

a.req('PATCH', f'/api/parties/{pid}', {'meet_place': '기숙사 앞'})
ev = recv_until(ws_b, 'party_updated')
check('수정 → B 화면 파티 정보 갱신', isinstance(ev, dict) and ev['party']['meet_place'] == '기숙사 앞')

a.req('DELETE', f'/api/parties/{pid}/members/{b.id}')
ev = recv_until(ws_b, 'kicked')
check('강퇴 → B에 kicked 알림', isinstance(ev, dict))
check('강퇴 → B 연결 끊김', recv_until(ws_b, 'never', 2) == 'CLOSED')

msgs = a.req('GET', f'/api/parties/{pid}/messages')[1]['messages']
check('채팅이 DB에 저장됨', any(m['type'] == 'chat' for m in msgs))

a.req('DELETE', f'/api/parties/{pid}')
ev = recv_until(ws_a, 'party_deleted')
check('삭제 → A에 party_deleted', isinstance(ev, dict))
ev = recv_until(lobby, 'party_removed')
check('삭제 → 목록에서 제거 알림', isinstance(ev, dict) and ev['party_id'] == pid)

print('\n실패', fails, '건')
import os
os._exit(1 if fails else 0)   # 열린 연결의 백그라운드 스레드를 기다리지 않고 종료
