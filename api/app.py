from flask import Flask, jsonify
app = Flask(__name__)
app.json.ensure_ascii = False

import os
import sqlite3
from pathlib import Path

@app.get('/api/health')
def health():
    return jsonify(status='ok')


DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'notice.db'

with sqlite3.connect(DB) as db:
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)