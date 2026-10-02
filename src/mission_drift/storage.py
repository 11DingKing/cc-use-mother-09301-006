"""SQLite 存储层。

设计约束：

* ``snapshots`` / ``snapshot_items`` 在快照封存（sealed_at 非空）后，
  由触发器阻止任何 UPDATE/DELETE，封存即法律意义上的冻结。
* 案件（cases）与审计轨迹（case_audit）各自带哈希链，
  每条记录的 hash 包含前一条 hash，任何事后篡改都会断链。
* 规则、豁免、评估运行只增不改，一律不提供 UPDATE/DELETE 接口。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS units (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id           TEXT PRIMARY KEY,
    ordinal      INTEGER NOT NULL UNIQUE,   -- 周期先后次序，规则据此界定生效范围
    label        TEXT NOT NULL,
    sealed       INTEGER NOT NULL DEFAULT 0,
    sealed_at    TEXT,
    created_at   TEXT NOT NULL
);

-- 五要素快照：使命/承诺目标/学科投入/招生结构/服务成果
CREATE TABLE IF NOT EXISTS snapshots (
    id           TEXT PRIMARY KEY,
    cycle_id     TEXT NOT NULL REFERENCES cycles(id),
    unit_id      TEXT NOT NULL REFERENCES units(id),
    kind         TEXT NOT NULL
                 CHECK (kind IN ('mission','commitment','discipline_input',
                                 'enrollment','service_output')),
    payload      TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    sealed_at    TEXT,
    hash         TEXT,
    UNIQUE(cycle_id, unit_id, kind)
);

CREATE TABLE IF NOT EXISTS rules (
    id            TEXT PRIMARY KEY,
    version       INTEGER NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    definition    TEXT NOT NULL,   -- 规则 DSL JSON
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','approved','retired')),
    effective_from_cycle TEXT REFERENCES cycles(id),
    retired_from_cycle   TEXT REFERENCES cycles(id),  -- 退役只对该周期起生效，旧周期重放仍适用
    approved_by   TEXT,
    approved_at   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exemptions (
    id             TEXT PRIMARY KEY,
    unit_id        TEXT NOT NULL REFERENCES units(id),
    rule_id        TEXT,                              -- NULL 表示对所有规则生效
    cycle_from     TEXT NOT NULL REFERENCES cycles(id),
    cycle_to       TEXT NOT NULL REFERENCES cycles(id),
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','revoked')),
    revoke_from_cycle   TEXT REFERENCES cycles(id),  -- 撤销只对未来周期生效
    granted_by     TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    revoked_by     TEXT,
    revoked_at     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id                 TEXT PRIMARY KEY,
    cycle_id           TEXT NOT NULL REFERENCES cycles(id),
    kind               TEXT NOT NULL DEFAULT 'official'
                       CHECK (kind IN ('official','replay')),
    triggered_by       TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    engine_version     TEXT NOT NULL,
    ruleset_hash       TEXT NOT NULL,
    exemptions_hash    TEXT NOT NULL,
    result_hash        TEXT,
    input_manifest     TEXT NOT NULL,
    parent_run_id      TEXT REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS run_findings (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    unit_id       TEXT NOT NULL REFERENCES units(id),
    rule_id       TEXT NOT NULL REFERENCES rules(id),
    rule_version  INTEGER NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('medium','high')),
    value_path    TEXT NOT NULL,
    observed      TEXT NOT NULL,
    threshold     TEXT NOT NULL,
    detail        TEXT NOT NULL,
    finding_hash  TEXT NOT NULL,
    UNIQUE(run_id, unit_id, rule_id)
);

CREATE TABLE IF NOT EXISTS cases (
    id               TEXT PRIMARY KEY,
    unit_id          TEXT NOT NULL REFERENCES units(id),
    cycle_id         TEXT NOT NULL REFERENCES cycles(id),
    rule_id          TEXT NOT NULL REFERENCES rules(id),
    rule_version     INTEGER NOT NULL,
    status           TEXT NOT NULL
                     CHECK (status IN ('alert','verifying','rectifying','closed')),
    finding_snapshot TEXT NOT NULL,      -- 立案依据的冻结 finding 副本
    finding_hash     TEXT NOT NULL,
    first_run_id     TEXT NOT NULL REFERENCES runs(id),
    latest_run_id    TEXT NOT NULL REFERENCES runs(id),
    explain_deadline TEXT,
    explain_submitted_at TEXT,
    explain_text     TEXT,
    exempt_deadline  TEXT,
    exempt_status    TEXT,              -- granted / denied / 无
    rectify_deadline TEXT,
    rectify_submitted_at TEXT,
    rectify_plan     TEXT,
    closed_reason    TEXT,
    prev_hash        TEXT,
    hash             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_audit (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    seq         INTEGER NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT,
    hash        TEXT NOT NULL,
    UNIQUE(case_id, seq)
);

-- 封存后快照不可改、不可删
CREATE TRIGGER IF NOT EXISTS trg_snapshot_sealed_update
BEFORE UPDATE ON snapshots
WHEN OLD.sealed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '快照已封存，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_snapshot_sealed_delete
BEFORE DELETE ON snapshots
WHEN OLD.sealed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '快照已封存，禁止删除');
END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：HTTP 服务多线程共享连接，由调用方加锁串行化写事务
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    keys = sorted(row)
    sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"
    conn.execute(sql, [_encode(row[k]) for k in keys])


def _encode(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return value


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, list(params)).fetchone()


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, list(params)).fetchall())
