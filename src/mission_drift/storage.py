"""SQLite 存储层。

只负责 schema、连接与事务，不含业务规则；业务编排放 services.py。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actors (
    id INTEGER PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planning','department','supervisor')),
    department_id INTEGER REFERENCES departments(id),
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    starts_on TEXT,
    ends_on TEXT,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS rule_sets (
    id INTEGER PRIMARY KEY,
    version INTEGER UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','retired')),
    rules_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    rationale TEXT,
    created_by INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL,
    approved_by INTEGER REFERENCES actors(id),
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    status TEXT NOT NULL CHECK(status IN ('open','sealed')),
    created_by INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL,
    sealed_by INTEGER REFERENCES actors(id),
    sealed_at TEXT,
    rule_set_version INTEGER,
    items_hash TEXT,
    findings_hash TEXT,
    prev_hash TEXT,
    chain_hash TEXT,
    UNIQUE(cycle_id, department_id)
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    kind TEXT NOT NULL CHECK(kind IN
        ('mission','commitments','investment','enrollment','service')),
    payload_json TEXT NOT NULL,
    updated_by INTEGER REFERENCES actors(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, kind)
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    case_no TEXT UNIQUE NOT NULL,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    rule_set_version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL CHECK(level IN ('watch','warn')),
    status TEXT NOT NULL CHECK(status IN ('监测','预警','核实','整改','关闭')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    findings_hash TEXT NOT NULL,
    explanation_due TEXT,
    explanation_submitted_at TEXT,
    rectification_due TEXT,
    rectification_submitted_at TEXT,
    exempt_until TEXT,
    opened_at TEXT NOT NULL,
    created_by INTEGER REFERENCES actors(id),
    closed_at TEXT,
    close_reason TEXT,
    UNIQUE(snapshot_id, rule_id, subject)
);

CREATE TABLE IF NOT EXISTS case_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    actor_id INTEGER,
    actor_name TEXT NOT NULL,
    role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(case_id, seq)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT NOT NULL,
    prev_hash TEXT,
    entry_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_lookup ON cases(cycle_id, department_id, status);
CREATE INDEX IF NOT EXISTS idx_events_case ON case_events(case_id, seq);
"""

SNAPSHOT_KINDS = ("mission", "commitments", "investment", "enrollment", "service")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        first = not Path(self.path).exists() if self.path != ":memory:" else False
        # 审计哈希链与快照链要求写操作全局有序，用一把可重入锁串行化。
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        if first:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version','1')"
            )
            self.conn.commit()

    def tx(self):
        return self.conn  # 调用方用 with store.conn: 包裹事务

    def close(self) -> None:
        self.conn.close()
