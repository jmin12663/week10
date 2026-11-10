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

CATEGORIES = ('notice', 'recruit')   # notice: 일반 공지, recruit: 선착순 모집글


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row   # 결과를 dict처럼 꺼낼 수 있게
    return db


NOTICE_TABLE_SQL = '''CREATE TABLE IF NOT EXISTS {name} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT    NOT NULL CHECK (category IN ('notice', 'recruit')),
    title      TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    capacity   INTEGER,                      -- 모집글만 사용, 공지는 NULL
    applied    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK ((category = 'notice' AND capacity IS NULL) OR
           (category = 'recruit' AND capacity > 0))
)'''

with closing(connect()) as db, db:
    db.execute(NOTICE_TABLE_SQL.format(name='notice'))

    # 이전 버전 DB(category 칸이 없던 때)면 새 구조의 테이블로 옮김.
    # SQLite는 기존 칸의 제약(capacity NOT NULL)을 바꿀 수 없어서 새로 만들어 복사.
    # 이전 글은 모두 정원이 있었으므로 모집글로 분류됨. id가 유지되어 신청 내역도 그대로 연결됨.
    columns = [r['name'] for r in db.execute('PRAGMA table_info(notice)')]
    if 'category' not in columns:
        db.execute(NOTICE_TABLE_SQL.format(name='notice_new'))
        db.execute('''INSERT INTO notice_new (id, category, title, content, capacity, applied, created_at)
                      SELECT id, 'recruit', title, content, capacity, applied, created_at FROM notice''')
        db.execute('DROP TABLE notice')
        db.execute('ALTER TABLE notice_new RENAME TO notice')

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


def post_json(row):
    """DB 행을 응답용 dict로. 모집글에만 정원·남은 자리를 채움."""
    post = dict(row)
    if post['category'] == 'recruit':
        post['remaining'] = post['capacity'] - post['applied']
    else:
        post['capacity'] = post['applied'] = post['remaining'] = None
    return post


@app.get('/api/health')
def health():
    return jsonify(status='ok', student_id='2224037', name='신정민')


@app.errorhandler(413)
def too_large(e):
    return error('입력 내용이 너무 큽니다.', 413)


# 관리자 로그인 확인
@app.post('/api/admin/login')
def admin_login():
    data = request.get_json(silent=True)
    password = data.get('password') if isinstance(data, dict) else None
    if ADMIN_PASSWORD == '' or password != ADMIN_PASSWORD:
        return error('비밀번호가 올바르지 않습니다.', 401)
    return jsonify(message='관리자 인증 성공')


def read_post_fields(data, category):
    """작성·수정 공통 입력 검사. (title, content, capacity, 에러응답) 반환."""
    title = text_field(data, 'title', 100)
    content = text_field(data, 'content', 5000)
    capacity = data.get('capacity')

    if title is None or content is None:
        return None, None, None, error('제목(100자 이내)과 내용(5000자 이내)을 입력하세요.', 400)

    if category == 'recruit':
        # bool은 int의 하위 타입이라 True가 1로 통과하는 것을 막음
        if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 10000:
            return None, None, None, error('모집 정원은 1~10000 사이의 정수로 입력하세요.', 400)
    else:
        capacity = None   # 공지는 정원 없음 (보내도 무시)

    return title, content, capacity, None


# 1. 글 작성 (관리자) - 공지 또는 모집글
@app.post('/api/notices')
def create_notice():
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)

    category = data.get('category')
    if category not in CATEGORIES:
        return error('글 종류는 공지(notice) 또는 모집(recruit)이어야 합니다.', 400)

    title, content, capacity, err = read_post_fields(data, category)
    if err:
        return err

    with closing(connect()) as db, db:
        cur = db.execute('INSERT INTO notice (category, title, content, capacity) VALUES (?, ?, ?, ?)',
                         (category, title, content, capacity))
        notice_id = cur.lastrowid
    return jsonify(id=notice_id, category=category, title=title, capacity=capacity), 201


# 2. 글 목록 (?category=notice 또는 recruit로 거르기, 없으면 전체)
@app.get('/api/notices')
def list_notices():
    category = request.args.get('category')
    if category is not None and category not in CATEGORIES:
        return error('알 수 없는 글 종류입니다.', 400)

    sql = 'SELECT id, category, title, capacity, applied, created_at FROM notice'
    params = ()
    if category:
        sql += ' WHERE category = ?'
        params = (category,)
    sql += ' ORDER BY id DESC'

    with closing(connect()) as db:
        rows = db.execute(sql, params).fetchall()
    return jsonify(notices=[post_json(r) for r in rows])


# 3. 글 상세
@app.get('/api/notices/<int:notice_id>')
def get_notice(notice_id):
    with closing(connect()) as db:
        row = db.execute('SELECT * FROM notice WHERE id = ?', (notice_id,)).fetchone()
    if row is None:
        return error('존재하지 않는 글입니다.', 404)
    return jsonify(post_json(row))


# 글 수정 (관리자) - 글 종류는 바꿀 수 없음
@app.put('/api/notices/<int:notice_id>')
def update_notice(notice_id):
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('JSON 형식으로 보내주세요.', 400)

    with closing(connect()) as db, db:
        post = db.execute('SELECT category FROM notice WHERE id = ?', (notice_id,)).fetchone()
        if post is None:
            return error('존재하지 않는 글입니다.', 404)
        category = post['category']
        if 'category' in data and data['category'] != category:
            return error('글 종류(공지/모집)는 수정할 수 없습니다.', 400)

        title, content, capacity, err = read_post_fields(data, category)
        if err:
            return err

        if category == 'recruit':
            # 이미 신청한 인원보다 정원을 줄이지 못하게 조건부 UPDATE
            # (검사와 수정을 한 문장으로 해서 그 사이에 신청이 끼어들어도 안전)
            cur = db.execute('UPDATE notice SET title = ?, content = ?, capacity = ? '
                             'WHERE id = ? AND applied <= ?',
                             (title, content, capacity, notice_id, capacity))
            if cur.rowcount == 0:
                applied = db.execute('SELECT applied FROM notice WHERE id = ?', (notice_id,)).fetchone()['applied']
                return error(f'이미 {applied}명이 신청해서 정원을 {applied}명보다 줄일 수 없습니다.', 400)
        else:
            db.execute('UPDATE notice SET title = ?, content = ? WHERE id = ?',
                       (title, content, notice_id))

        row = db.execute('SELECT * FROM notice WHERE id = ?', (notice_id,)).fetchone()
    return jsonify(post_json(row))


# 글 삭제 (관리자) - 모집글이면 신청 내역도 함께 삭제
@app.delete('/api/notices/<int:notice_id>')
def delete_notice(notice_id):
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)

    with closing(connect()) as db, db:
        if db.execute('SELECT 1 FROM notice WHERE id = ?', (notice_id,)).fetchone() is None:
            return error('존재하지 않는 글입니다.', 404)
        removed = db.execute('DELETE FROM application WHERE notice_id = ?', (notice_id,)).rowcount
        db.execute('DELETE FROM notice WHERE id = ?', (notice_id,))
    return jsonify(message='삭제되었습니다.', id=notice_id, deleted_applications=removed)


# 4. 선착순 신청 (모집글만)
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
            post = db.execute('SELECT category FROM notice WHERE id = ?', (notice_id,)).fetchone()
            if post is None:
                return error('존재하지 않는 글입니다.', 404)
            if post['category'] != 'recruit':
                return error('신청할 수 없는 공지입니다.', 400)

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
            order = db.execute('SELECT applied FROM notice WHERE id = ?', (notice_id,)).fetchone()['applied']
    except sqlite3.IntegrityError:
        # 같은 학번이 동시에 두 번 요청한 경우: UNIQUE 제약이 막고, with 블록이 UPDATE까지 롤백
        return error('이미 신청한 학번입니다.', 409)

    return jsonify(message=f'{order}번째로 신청이 완료되었습니다.', order=order,
                   notice_id=notice_id, student_id=student_id), 201


# 5. 신청자 명단 (관리자, 모집글만)
@app.get('/api/notices/<int:notice_id>/applications')
def list_applications(notice_id):
    if not is_admin():
        return error('관리자 비밀번호가 올바르지 않습니다.', 401)
    with closing(connect()) as db:
        post = db.execute('SELECT id, category, title, capacity, applied FROM notice WHERE id = ?',
                          (notice_id,)).fetchone()
        if post is None:
            return error('존재하지 않는 글입니다.', 404)
        if post['category'] != 'recruit':
            return error('모집글이 아니어서 신청자가 없습니다.', 400)
        rows = db.execute('SELECT student_id, name, created_at FROM application '
                          'WHERE notice_id = ? ORDER BY id', (notice_id,)).fetchall()
    return jsonify(notice=dict(post), applications=[dict(r) for r in rows])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)