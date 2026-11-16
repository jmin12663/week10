from flask import Blueprint, jsonify, request, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import closing
import re
import secrets
import sqlite3

from database import connect, now_kst
from common import error, read_json, text_field, login_required, DELETED_NAME
from parties import now_minute

bp = Blueprint('auth', __name__)

USERNAME_RE = re.compile(r'[a-z0-9]{4,20}')
STUDENT_ID_RE = re.compile(r'\d{7,10}')     # 학교 학번 자릿수에 맞게 조정

# 없는 아이디로 로그인해도 비밀번호 검사 시간을 똑같이 써서,
# 응답 시간 차이로 "존재하는 아이디"를 알아내지 못하게 함
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


# 회원가입
@bp.post('/api/users')
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
@bp.post('/api/sessions')
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
@bp.delete('/api/sessions/current')
def logout():
    session.clear()
    return '', 204


# 내 정보 (본인에게는 이름을 가리지 않고 보여줌)
@bp.get('/api/users/me')
@login_required
def me():
    return jsonify(me_json(g.user))


def me_json(u):
    return {'id': u['id'], 'username': u['username'], 'student_id': u['student_id'],
            'department': u['department'], 'name': u['name']}


# 내 정보 수정: 학과만 바꿀 수 있음.
# 이름·학번은 정산 안 한 사람을 확인하는 근거라 바꾸지 못하게 함 (잘못 가입했으면 탈퇴 후 재가입)
@bp.patch('/api/users/me')
@login_required
def update_me():
    data = read_json()
    if data is None:
        return error('JSON 형식으로 보내주세요.', 400)
    department = text_field(data, 'department', 30)
    if department is None:
        return error('학과를 30자 이내로 입력하세요.', 400)
    with closing(connect()) as db, db:
        db.execute('UPDATE users SET department = ? WHERE id = ?', (department, g.user['id']))
        user = db.execute('SELECT * FROM users WHERE id = ?', (g.user['id'],)).fetchone()
    return jsonify(me_json(user))


# 비밀번호 변경: 현재 비밀번호를 한 번 더 확인
@bp.put('/api/users/me/password')
@login_required
def change_password():
    data = read_json()
    if data is None:
        return error('JSON 형식으로 보내주세요.', 400)
    current = data.get('current_password')
    new = data.get('new_password')
    if not isinstance(current, str) or not check_password_hash(g.user['password_hash'], current):
        return error('현재 비밀번호가 올바르지 않습니다.', 403)
    if not isinstance(new, str) or not 8 <= len(new) <= 64:
        return error('새 비밀번호는 8~64자로 입력하세요.', 400)
    with closing(connect()) as db, db:
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                   (generate_password_hash(new), g.user['id']))
    return '', 204


# 회원 탈퇴: 계정 행은 지우지 않고 개인정보만 지움.
# 지난 파티의 참여자 기록·채팅이 다른 사람에게 그대로 남아야 하고, 외래키도 깨지지 않게 하기 위함.
@bp.delete('/api/users/me')
@login_required
def delete_me():
    data = read_json()
    password = data.get('password') if data else None
    if not isinstance(password, str) or not check_password_hash(g.user['password_hash'], password):
        return error('비밀번호가 올바르지 않습니다.', 403)

    me_id = g.user['id']
    with closing(connect()) as db, db:
        # 출발 전인 파티에 남아 있으면 인원 표시가 틀어지므로 먼저 나가거나 삭제하게 함
        active = db.execute('''SELECT 1 FROM party_member m JOIN party p ON p.id = m.party_id
                               WHERE m.user_id = ? AND p.depart_at >= ?''',
                            (me_id, now_minute())).fetchone()
        if active:
            return error('출발 전인 파티가 있습니다. 파티에서 나가거나 모집글을 삭제한 뒤 탈퇴하세요.', 409)
        # 아이디·학번 자리는 UNIQUE라 'deleted-<id>'로 비워서 같은 아이디·학번으로 재가입 가능하게 함.
        # 비밀번호 해시를 '!'로 바꾸면 어떤 비밀번호로도 로그인되지 않음.
        db.execute('''UPDATE users SET username = ?, student_id = ?, name = ?, department = '',
                      password_hash = '!' WHERE id = ?''',
                   (f'deleted-{me_id}', f'deleted-{me_id}', DELETED_NAME, me_id))
    session.clear()
    return '', 204
