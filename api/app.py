from flask import Flask, jsonify
from datetime import timedelta
import secrets

from database import DATA_DIR
from common import error
from realtime import sock
import auth
import parties

app = Flask(__name__)
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
# 25초마다 ping을 보내 대화가 없어도 연결이 끊기지 않게 유지
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}

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

app.register_blueprint(auth.bp)
app.register_blueprint(parties.bp)
sock.init_app(app)


@app.errorhandler(413)
def too_large(e):
    return error('입력 내용이 너무 큽니다.', 413)


@app.get('/api/health')
def health():
    return jsonify(status='ok', student_id='2224037', name='신정민')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
