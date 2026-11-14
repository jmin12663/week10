from flask import Blueprint, jsonify, request, g
from contextlib import closing
from datetime import datetime, timedelta
import sqlite3

from database import connect, now_kst, is_member, KST, CAPACITY
from common import error, read_json, text_field, mask_name, public_user, login_required
from realtime import broadcast_room, broadcast_lobby, close_room, close_room_member

bp = Blueprint('parties', __name__)

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
    """참여자 목록. 학번은 같은 파티 참여자에게만 보여줌 (정산 안 된 사람 확인용)
    → 파티 상세(참여자만)와 채팅방 알림(참여 확인된 연결만)으로만 나감."""
    rows = db.execute('''SELECT u.id, u.name, u.department, u.student_id, m.joined_at
                         FROM party_member m JOIN users u ON u.id = m.user_id
                         WHERE m.party_id = ? ORDER BY m.id''', (party_id,)).fetchall()
    return [{**public_user(r), 'student_id': r['student_id'], 'is_host': r['id'] == host_id,
             'joined_at': r['joined_at']} for r in rows]


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
@bp.get('/api/parties')
@login_required
def list_parties():
    me_id = g.user['id']
    # ?joined=1: 내가 참여 중인 파티만 / ?past=1: 참여했던 파티 중 출발 시간이 지난 것 (지난 파티 기록)
    past = request.args.get('past') == '1'
    joined_only = request.args.get('joined') == '1'
    sql = PARTY_SELECT + (' WHERE p.depart_at < :now' if past else ' WHERE p.depart_at >= :now')
    if past or joined_only:
        sql += ' AND EXISTS (SELECT 1 FROM party_member m WHERE m.party_id = p.id AND m.user_id = :me)'
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

    if past:
        # ponytail: 최근 50개만. 더 보기가 필요해지면 before 커서로 페이지 나누기
        sql += ' ORDER BY p.depart_at DESC, p.id DESC LIMIT 50'
    else:
        # 내 파티 → 참여 중 → 나머지, 각 묶음 안에서는 출발 시간순
        sql += ' ORDER BY p.host_user_id = :me DESC, joined DESC, p.depart_at, p.id'
    with closing(connect()) as db:
        rows = db.execute(sql, params).fetchall()
    return jsonify(parties=[party_json(r, me_id) for r in rows])


# 파티 생성: 작성자가 방장이 되고 자동으로 첫 참여자가 됨
@bp.post('/api/parties')
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
@bp.get('/api/parties/<int:party_id>')
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
@bp.patch('/api/parties/<int:party_id>')
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
@bp.delete('/api/parties/<int:party_id>')
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
@bp.post('/api/parties/<int:party_id>/members')
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
@bp.delete('/api/parties/<int:party_id>/members/<target>')
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
        party = db.execute('SELECT host_user_id, depart_at FROM party WHERE id = ?',
                           (party_id,)).fetchone()
        if party is None:
            return error('존재하지 않는 파티입니다.', 404)
        is_host = party['host_user_id'] == me_id

        if leaving and is_host:
            return error('방장은 나갈 수 없습니다. 모집글을 삭제하세요.', 409)
        if not leaving and not is_host:
            return error('방장만 강퇴할 수 있습니다.', 403)
        # 출발 후에는 참여자 기록을 그대로 남김 (지난 파티 기록에서 빠지지 않게)
        if party['depart_at'] < now_minute():
            return error('출발 시간이 지난 파티는 나가거나 강퇴할 수 없습니다.', 409)

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


@bp.get('/api/parties/<int:party_id>/messages')
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
