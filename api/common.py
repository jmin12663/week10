from flask import jsonify, request, session, g
from contextlib import closing
from functools import wraps

from database import connect


def error(message, status):
    return jsonify(error=message), status


def read_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


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
