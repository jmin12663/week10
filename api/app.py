from flask import Flask, jsonify, request
from contextlib import closing
from pathlib import Path
import os
import sqlite3

app = Flask(__name__)
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'notice.db'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row   # 결과를 dict처럼 꺼낼 수 있게
    return db


with closing(connect()) as db, db:
    db.execute('''CREATE TABLE IF NOT EXISTS notice (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT    NOT NULL,
        content    TEXT    NOT NULL,
        capacity   INTEGER NOT NULL CHECK (capacity > 0),
        applied    INTEGER NOT NULL DEFAULT 0,
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS application (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        notice_id  INTEGER NOT NULL REFERENCES notice(id),
        student_id TEXT    NOT NULL,
        name       TEXT    NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (notice_id, student_id)
    )''')


def is_admin():
    return ADMIN_PASSWORD != '' and request.headers.get('X-Admin-Password') == ADMIN_PASSWORD


def error(message, status):
    return jsonify(error=message), status


def text_field(data, key, max_len):
    """문자열 필드를 꺼내 공백 제거 후 반환. 비었거나 너무 길면 None."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        return None
    return value.strip()


@app.get('/api/health')
def health():
    return jsonify(status='ok', student_id='2224037', name='신정민')


@app.errorhandler(413)
def too_large(e):
    return error('입력 내용이 너무 큽니다.', 413)


# 1. 공지 작성 (관리자)
@app.post('/api/notices')
def create_notice():
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)

    title = text_field(data, 'title', 100)
    content = text_field(data, 'content', 5000)
    capacity = data.get('capacity')

    if title is None or content is None:
        return error('제목(100자 이내)과 내용(5000자 이내)을 입력하세요.', 400)
    # bool은 int의 하위 타입이라 True가 1로 통과하는 것을 막음
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 10000:
        return error('정원은 1~10000 사이의 정수로 입력하세요.', 400)

    with closing(connect()) as db, db:
        cur = db.execute('INSERT INTO notice (title, content, capacity) VALUES (?, ?, ?)',
                         (title, content, capacity))
        notice_id = cur.lastrowid
    return jsonify(id=notice_id, title=title, capacity=capacity), 201


# 2. 공지 목록
@app.get('/api/notices')
def list_notices():
    with closing(connect()) as db:
        rows = db.execute('SELECT id, title, capacity, applied, created_at '
                          'FROM notice ORDER BY id DESC').fetchall()
    return jsonify(notices=[dict(r) | {'remaining': r['capacity'] - r['applied']} for r in rows])


# 3. 공지 상세
@app.get('/api/notices/<int:notice_id>')
def get_notice(notice_id):
    with closing(connect()) as db:
        row = db.execute('SELECT * FROM notice WHERE id = ?', (notice_id,)).fetchone()
    if row is None:
        return error('존재하지 않는 공지입니다.', 404)
    return jsonify(dict(row) | {'remaining': row['capacity'] - row['applied']})


# 4. 선착순 신청
@app.post('/api/notices/<int:notice_id>/apply')
def apply(notice_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)

    student_id = text_field(data, 'student_id', 20)
    name = text_field(data, 'name', 30)
    if student_id is None or not student_id.isdigit() or name is None:
        return error('학번(숫자)과 이름을 입력하세요.', 400)

    try:
        with closing(connect()) as db, db:
            if db.execute('SELECT 1 FROM notice WHERE id = ?', (notice_id,)).fetchone() is None:
                return error('존재하지 않는 공지입니다.', 404)

            if db.execute('SELECT 1 FROM application WHERE notice_id = ? AND student_id = ?',
                          (notice_id, student_id)).fetchone():
                return error('이미 신청한 학번입니다.', 409)

            # 정원 확인과 증가를 한 문장으로 처리 → 동시에 요청이 와도 정원 초과 불가
            cur = db.execute('UPDATE notice SET applied = applied + 1 '
                             'WHERE id = ? AND applied < capacity', (notice_id,))
            if cur.rowcount == 0:
                return error('신청이 마감되었습니다.', 409)

            db.execute('INSERT INTO application (notice_id, student_id, name) VALUES (?, ?, ?)',
                       (notice_id, student_id, name))
    except sqlite3.IntegrityError:
        # 같은 학번이 동시에 두 번 요청한 경우: UNIQUE 제약이 막고, with 블록이 UPDATE까지 롤백
        return error('이미 신청한 학번입니다.', 409)

    return jsonify(message='신청이 완료되었습니다.', notice_id=notice_id, student_id=student_id), 201


# 5. 신청자 명단 (관리자)
@app.get('/api/notices/<int:notice_id>/applications')
def list_applications(notice_id):
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)
    with closing(connect()) as db:
        notice = db.execute('SELECT id, title, capacity, applied FROM notice WHERE id = ?',
                            (notice_id,)).fetchone()
        if notice is None:
            return error('존재하지 않는 공지입니다.', 404)
        rows = db.execute('SELECT student_id, name, created_at FROM application '
                          'WHERE notice_id = ? ORDER BY id', (notice_id,)).fetchall()
    return jsonify(notice=dict(notice), applications=[dict(r) for r in rows])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)