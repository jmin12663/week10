from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'taxi.db'          # 이전 notice.db와 분리해서 새로 시작

KST = timezone(timedelta(hours=9))  # 컨테이너 기본 시간이 UTC라 한국 시간을 직접 지정
CAPACITY = 4                        # 파티 정원 고정


def now_kst():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')   # SQLite는 외래키 검사가 기본으로 꺼져 있음
    return db


def is_member(db, party_id, user_id):
    return db.execute('SELECT 1 FROM party_member WHERE party_id = ? AND user_id = ?',
                      (party_id, user_id)).fetchone() is not None


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
