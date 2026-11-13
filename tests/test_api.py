"""REST API 시험 (동시 입장 포함). 사용법: python3 tests/test_api.py http://localhost"""
import json, threading, urllib.request, urllib.parse, http.cookiejar
from datetime import datetime, timedelta, timezone

import sys
import random
RUN = random.randint(100, 999)   # 실행마다 다른 시험 계정
B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5000'
KST = timezone(timedelta(hours=9))


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(B + urllib.parse.quote(path, safe='/?=&'), data=data, method=method,
                                   headers={'Content-Type': 'application/json'})
        try:
            with self.op.open(r) as res:
                txt = res.read().decode()
                return res.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            return e.code, (json.loads(txt) if txt else None)


def user(n):
    c = Client()
    c.req('POST', '/api/users', {'username': f'test{RUN}u{n}', 'password': 'password1',
                                 'student_id': f'9{RUN}{n:03d}', 'department': '컴퓨터공학과',
                                 'name': f'김철수{n}'})
    c.req('POST', '/api/sessions', {'username': f'test{RUN}u{n}', 'password': 'password1'})
    return c


def t(label, res, expect):
    ok = '✅' if res[0] == expect else '❌'
    extra = res[1].get('error', '') if isinstance(res[1], dict) else ''
    print(f'{ok} {label}: {res[0]} {extra}')


later = (datetime.now(KST) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
later2 = (datetime.now(KST) + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
past = (datetime.now(KST) - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
far = (datetime.now(KST) + timedelta(days=8)).strftime('%Y-%m-%d %H:%M')

host = user(1)
users = [user(n) for n in range(2, 9)]
anon = Client()

t('비로그인 목록', anon.req('GET', '/api/parties'), 401)
t('생성 정상', s := host.req('POST', '/api/parties', {'meet_place': '정문', 'destination': '양주역', 'depart_at': later}), 201)
pid = s[1]['id']
print('   ', s[1])
t('생성 지난 시간', host.req('POST', '/api/parties', {'meet_place': '정문', 'destination': '양주역', 'depart_at': past}), 400)
t('생성 8일 뒤', host.req('POST', '/api/parties', {'meet_place': '정문', 'destination': '양주역', 'depart_at': far}), 400)
t('생성 빈 목적지', host.req('POST', '/api/parties', {'meet_place': '정문', 'destination': ' ', 'depart_at': later}), 400)
t('검색 양주', r := users[0].req('GET', '/api/parties?destination=양주'), 200); print('    결과', len(r[1]['parties']))
t('검색 서울', r := users[0].req('GET', '/api/parties?destination=서울'), 200); print('    결과', len(r[1]['parties']))
t('검색 날짜 오류', users[0].req('GET', '/api/parties?date=11-20'), 400)
t('비참여자 상세', users[0].req('GET', f'/api/parties/{pid}'), 403)

# 동시 입장: 방장 1명 있는 방에 7명이 동시에
results = []
def join(c): results.append(c.req('POST', f'/api/parties/{pid}/members')[0])
ths = [threading.Thread(target=join, args=(c,)) for c in users]
[th.start() for th in ths]; [th.join() for th in ths]
print(f'{"✅" if sorted(results) == [201]*3 + [409]*4 else "❌"} 동시 입장 7명:', sorted(results))
d = host.req('GET', f'/api/parties/{pid}')
print('    인원', d[1]['party']['member_count'], [m['name'] for m in d[1]['members']])

joined = [c for c in users if c.req('GET', f'/api/parties/{pid}')[0] == 200]
outsider = [c for c in users if c not in joined][0]
member = joined[0]
member_id = member.req('GET', '/api/users/me')[1]['id']

t('이미 참여 중 재입장', member.req('POST', f'/api/parties/{pid}/members'), 409)
t('정원 초과 입장', outsider.req('POST', f'/api/parties/{pid}/members'), 409)
t('방장 아닌 수정', member.req('PATCH', f'/api/parties/{pid}', {'destination': '서울'}), 403)
t('수정 정상', r := host.req('PATCH', f'/api/parties/{pid}', {'depart_at': later2, 'meet_place': '기숙사 앞'}), 200)
t('수정 항목 없음', host.req('PATCH', f'/api/parties/{pid}', {}), 400)
t('방장 아닌 강퇴', member.req('DELETE', f'/api/parties/{pid}/members/{member_id}'.replace(str(member_id), '1')), 403)
t('방장 나가기', host.req('DELETE', f'/api/parties/{pid}/members/me'), 409)
t('강퇴 정상', host.req('DELETE', f'/api/parties/{pid}/members/{member_id}'), 204)
t('강퇴자 재입장', member.req('POST', f'/api/parties/{pid}/members'), 403)
t('빈자리에 외부인 입장', outsider.req('POST', f'/api/parties/{pid}/members'), 201)
t('나가기 정상', joined[1].req('DELETE', f'/api/parties/{pid}/members/me'), 204)
t('나간 뒤 또 나가기', joined[1].req('DELETE', f'/api/parties/{pid}/members/me'), 404)
t('채팅 기록', r := host.req('GET', f'/api/parties/{pid}/messages'), 200)
for m in r[1]['messages']:
    print('    [시스템]', m['content'])
t('강퇴자 채팅 기록', member.req('GET', f'/api/parties/{pid}/messages'), 403)
t('방장 아닌 삭제', outsider.req('DELETE', f'/api/parties/{pid}'), 403)
t('삭제 정상', host.req('DELETE', f'/api/parties/{pid}'), 204)
t('삭제 후 상세', host.req('GET', f'/api/parties/{pid}'), 404)
t('삭제된 글 삭제', host.req('DELETE', f'/api/parties/{pid}'), 404)
