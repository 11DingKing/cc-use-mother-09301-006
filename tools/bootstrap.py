"""初始化/引导：在空库中建立三个角色的初始账号。

用法：
  python tools/bootstrap.py --db mission_drift.sqlite3
首个发展规划处账号只能由此脚本离线创建（接口不允许自举权限）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift.clock import SystemClock
from mission_drift.services import (
    ROLE_DEPARTMENT,
    ROLE_PLANNING,
    ROLE_SUPERVISOR,
    Services,
)
from mission_drift.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化角色账号")
    parser.add_argument("--db", default="mission_drift.sqlite3")
    args = parser.parse_args()

    store = Store(args.db)
    svc = Services(store, SystemClock())
    existing = svc.conn.execute("SELECT COUNT(*) AS n FROM actors").fetchone()["n"]
    if existing:
        print("库中已有账号，引导中止（如需重置请使用空库文件）。", file=sys.stderr)
        sys.exit(1)

    now = svc.clock.now().isoformat()
    seeds = [
        ("bootstrap-planning", "发展规划处-初始管理员", ROLE_PLANNING, None),
        ("bootstrap-supervisor", "校级督导-初始督导", ROLE_SUPERVISOR, None),
    ]
    out = []
    with svc.conn:
        for token, name, role, dept in seeds:
            cur = svc.conn.execute(
                "INSERT INTO actors(token,name,role,department_id,created_at)"
                " VALUES(?,?,?,?,?)",
                (token, name, role, dept, now),
            )
            svc.audit.append(now, {"id": cur.lastrowid, "name": name, "role": role},
                             "actor.bootstrap", f"actor:{name}", {"role": role})
            out.append({"name": name, "role": role, "token": token})
    store.close()
    print(json.dumps({"created": out}, ensure_ascii=False, indent=2))
    print("请立即通过 POST /api/actors 建立日常账号并停用/替换初始令牌。",
          file=sys.stderr)


if __name__ == "__main__":
    main()
