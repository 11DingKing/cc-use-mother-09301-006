"""业务编排层：认证、周期、规则版本、快照封存、案件工作流、重放。

所有跨表写操作都在单个事务内完成；所有写入均留痕
（audit_log 全库哈希链 + case_events 案件内追加日志）。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, timedelta
from typing import Any, Callable

from .clock import Clock
from .engine import (
    evaluate,
    findings_hash,
    validate_payload,
    validate_rule,
    validate_settings,
)
from .errors import ConflictError, DomainError, NotFoundError, PermissionError_
from .hashing import canonical_json, chain_digest, digest
from .storage import SNAPSHOT_KINDS, Store

ROLE_PLANNING = "planning"
ROLE_DEPARTMENT = "department"
ROLE_SUPERVISOR = "supervisor"
ROLE_NAMES = {
    ROLE_PLANNING: "发展规划处",
    ROLE_DEPARTMENT: "院系负责人",
    ROLE_SUPERVISOR: "校级督导",
}

STATE_MONITOR = "监测"
STATE_WARN = "预警"
STATE_VERIFY = "核实"
STATE_RECTIFY = "整改"
STATE_CLOSED = "关闭"


def _iso_d(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise DomainError(f"日期格式应为 YYYY-MM-DD：{text}") from exc
    return text


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class AuditTrail:
    """全库追加式审计链：每条摘要串入上一条，封库可验。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(self, at: str, actor: dict | None, action: str,
               target: str | None, detail: dict) -> None:
        prev = self.conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["entry_hash"] if prev else None
        body = {
            "at": at,
            "actor": actor["name"] if actor else None,
            "role": actor["role"] if actor else None,
            "action": action,
            "target": target,
            "detail": detail,
        }
        entry_hash = chain_digest(prev_hash, body)
        self.conn.execute(
            "INSERT INTO audit_log(at,actor_id,action,target,detail_json,"
            "prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?)",
            (at, actor["id"] if actor else None, action, target,
             canonical_json(body["detail"]), prev_hash, entry_hash),
        )

    def verify(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY seq"
        ).fetchall()
        prev_hash = None
        for row in rows:
            detail = json.loads(row["detail_json"])
            actor_row = self.conn.execute(
                "SELECT name,role FROM actors WHERE id=?", (row["actor_id"],)
            ).fetchone() if row["actor_id"] else None
            body = {
                "at": row["at"],
                "actor": actor_row["name"] if actor_row else None,
                "role": actor_row["role"] if actor_row else None,
                "action": row["action"],
                "target": row["target"],
                "detail": detail,
            }
            expect = chain_digest(prev_hash, body)
            if expect != row["entry_hash"] or row["prev_hash"] != prev_hash:
                raise ConflictError(f"审计链在 seq={row['seq']} 处断裂")
            prev_hash = row["entry_hash"]
        return {"entries": len(rows), "intact": True}


class Services:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.conn = store.conn
        self.clock = clock
        self.audit = AuditTrail(self.conn)

    # ---------- 认证 ----------

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise PermissionError_("缺少 X-Auth-Token")
        row = self.conn.execute(
            "SELECT * FROM actors WHERE token=? AND disabled=0", (token,)
        ).fetchone()
        if not row:
            raise PermissionError_("令牌无效或已停用")
        return dict(row)

    def require_role(self, actor: dict, *roles: str) -> None:
        if actor["role"] not in roles:
            raise DomainError(
                f"需要角色：{'/'.join(ROLE_NAMES[r] for r in roles)}", 403
            )

    # ---------- 组织与账号 ----------

    def create_department(self, actor: dict, code: str, name: str) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        if not code or not name:
            raise DomainError("院系 code/name 不能为空")
        with self.conn:
            try:
                cur = self.conn.execute(
                    "INSERT INTO departments(code,name,created_at) VALUES(?,?,?)",
                    (code, name, self.clock.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"院系编号已存在：{code}") from exc
            dept = dict(self.conn.execute(
                "SELECT * FROM departments WHERE id=?", (cur.lastrowid,)
            ).fetchone())
            self.audit.append(self.clock.now().isoformat(), actor,
                              "department.create", f"department:{code}",
                              {"code": code, "name": name})
        return dept

    def list_departments(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM departments ORDER BY code").fetchall()]

    def create_actor(self, actor: dict, name: str, role: str,
                     department_code: str | None = None,
                     token: str | None = None) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        if role not in ROLE_NAMES:
            raise DomainError("role 只能是 planning/department/supervisor")
        dept_id = None
        if role == ROLE_DEPARTMENT:
            if not department_code:
                raise DomainError("院系账号必须绑定 department_code")
            dept = self._department_by_code(department_code)
            dept_id = dept["id"]
        token = token or secrets.token_urlsafe(18)
        with self.conn:
            try:
                cur = self.conn.execute(
                    "INSERT INTO actors(token,name,role,department_id,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (token, name, role, dept_id, self.clock.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("令牌冲突，请重试") from exc
            row = dict(self.conn.execute(
                "SELECT * FROM actors WHERE id=?", (cur.lastrowid,)).fetchone())
            self.audit.append(self.clock.now().isoformat(), actor,
                              "actor.create", f"actor:{row['id']}",
                              {"name": name, "role": role,
                               "department_code": department_code})
        return row

    def _department_by_code(self, code: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM departments WHERE code=?", (code,)).fetchone()
        if not row:
            raise NotFoundError(f"院系不存在：{code}")
        return dict(row)

    def disable_actor(self, actor: dict, actor_id: int) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        if actor_id == actor["id"]:
            raise ConflictError("不能停用自己的账号")
        row = self.conn.execute("SELECT * FROM actors WHERE id=?",
                                (actor_id,)).fetchone()
        if not row:
            raise NotFoundError(f"账号不存在：{actor_id}")
        with self.conn:
            self.conn.execute("UPDATE actors SET disabled=1 WHERE id=?",
                              (actor_id,))
            self.audit.append(self.clock.now().isoformat(), actor,
                              "actor.disable", f"actor:{actor_id}",
                              {"name": row["name"], "role": row["role"]})
        return {"id": actor_id, "disabled": True}

    # ---------- 周期 ----------

    def create_cycle(self, actor: dict, code: str, name: str,
                     starts_on: str | None, ends_on: str | None) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        starts_on, ends_on = _iso_d(starts_on), _iso_d(ends_on)
        if starts_on and ends_on and starts_on > ends_on:
            raise DomainError("周期开始日不能晚于结束日")
        with self.conn:
            try:
                cur = self.conn.execute(
                    "INSERT INTO cycles(code,name,starts_on,ends_on,status,"
                    "created_at) VALUES(?,?,?,?, 'open',?)",
                    (code, name, starts_on, ends_on,
                     self.clock.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"周期编号已存在：{code}") from exc
            row = dict(self.conn.execute(
                "SELECT * FROM cycles WHERE id=?", (cur.lastrowid,)).fetchone())
            self.audit.append(self.clock.now().isoformat(), actor,
                              "cycle.create", f"cycle:{code}",
                              {"code": code, "name": name})
        return row

    def close_cycle(self, actor: dict, code: str) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        with self.conn:
            cycle = self._cycle_row(code)
            if cycle["status"] == "closed":
                raise ConflictError("周期已关闭")
            self.conn.execute(
                "UPDATE cycles SET status='closed', closed_at=? WHERE id=?",
                (self.clock.now().isoformat(), cycle["id"]),
            )
            self.audit.append(self.clock.now().isoformat(), actor,
                              "cycle.close", f"cycle:{code}", {})
        return self._cycle_row(code)

    def list_cycles(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM cycles ORDER BY code").fetchall()]

    def _cycle_row(self, code: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM cycles WHERE code=?", (code,)).fetchone()
        if not row:
            raise NotFoundError(f"周期不存在：{code}")
        return dict(row)

    # ---------- 规则版本 ----------

    def create_rule_set(self, actor: dict, rules: list[dict],
                        settings: dict, rationale: str | None) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        if not isinstance(rules, list) or not rules:
            raise DomainError("rules 必须是非空列表")
        ids = [r.get("id") for r in rules]
        if len(ids) != len(set(ids)):
            raise DomainError("规则 id 不能重复")
        for rule in rules:
            validate_rule(rule)
        validate_settings(settings)
        with self.conn:
            current = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS m FROM rule_sets"
            ).fetchone()["m"]
            version = current + 1
            self.conn.execute(
                "INSERT INTO rule_sets(version,status,rules_json,settings_json,"
                "rationale,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (version, "draft", canonical_json(rules),
                 canonical_json(settings), rationale, actor["id"],
                 self.clock.now().isoformat()),
            )
            self.audit.append(self.clock.now().isoformat(), actor,
                              "rule_set.create", f"rule_set:v{version}",
                              {"version": version, "rule_count": len(rules)})
        return self.get_rule_set(version)

    def update_rule_set_draft(self, actor: dict, version: int,
                              rules: list[dict], settings: dict,
                              rationale: str | None) -> dict:
        self.require_role(actor, ROLE_PLANNING)
        row = self._rule_set_row(version)
        if row["status"] != "draft":
            raise ConflictError("只有草稿规则集可以修改")
        for rule in rules:
            validate_rule(rule)
        validate_settings(settings)
        with self.conn:
            self.conn.execute(
                "UPDATE rule_sets SET rules_json=?, settings_json=?, "
                "rationale=? WHERE version=?",
                (canonical_json(rules), canonical_json(settings),
                 rationale, version),
            )
            self.audit.append(self.clock.now().isoformat(), actor,
                              "rule_set.update", f"rule_set:v{version}",
                              {"rule_count": len(rules)})
        return self.get_rule_set(version)

    def approve_rule_set(self, actor: dict, version: int, note: str | None) -> dict:
        """职责分离：起草（规划）与批准（督导）必须是不同账号。"""
        self.require_role(actor, ROLE_SUPERVISOR)
        with self.conn:
            row = self._rule_set_row(version)
            if row["status"] != "draft":
                raise ConflictError("只有草稿规则集可以批准")
            if row["created_by"] == actor["id"]:
                raise ConflictError("起草人与批准人不能为同一人")
            self.conn.execute(
                "UPDATE rule_sets SET status='approved', approved_by=?, "
                "approved_at=? WHERE version=?",
                (actor["id"], self.clock.now().isoformat(), version),
            )
            self.audit.append(self.clock.now().isoformat(), actor,
                              "rule_set.approve", f"rule_set:v{version}",
                              {"note": note})
        return self.get_rule_set(version)

    def list_rule_sets(self) -> list[dict]:
        out = []
        for r in self.conn.execute(
            "SELECT id,version,status,rationale,created_by,created_at,"
            "approved_by,approved_at FROM rule_sets ORDER BY version"
        ).fetchall():
            d = dict(r)
            d["rules"] = json.loads(self.conn.execute(
                "SELECT rules_json FROM rule_sets WHERE version=?",
                (d["version"],)).fetchone()["rules_json"])
            out.append(d)
        return out

    def get_rule_set(self, version: int) -> dict:
        row = self._rule_set_row(version)
        return {
            "version": row["version"],
            "status": row["status"],
            "rules": json.loads(row["rules_json"]),
            "settings": json.loads(row["settings_json"]),
            "rationale": row["rationale"],
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
        }

    def _rule_set_row(self, version: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM rule_sets WHERE version=?", (version,)).fetchone()
        if not row:
            raise NotFoundError(f"规则版本不存在：v{version}")
        return row

    def _current_approved_rule_set(self) -> dict:
        row = self.conn.execute(
            "SELECT * FROM rule_sets WHERE status='approved' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise ConflictError("尚无经批准的规则集，无法封存/评估")
        return {
            "version": row["version"],
            "rules": json.loads(row["rules_json"]),
            "settings": json.loads(row["settings_json"]),
        }

    # ---------- 快照 ----------

    def _snapshot_row(self, cycle_code: str, dept_code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT s.* FROM snapshots s JOIN cycles c ON c.id=s.cycle_id "
            "JOIN departments d ON d.id=s.department_id "
            "WHERE c.code=? AND d.code=?",
            (cycle_code, dept_code),
        ).fetchone()
        if not row:
            raise NotFoundError(f"快照不存在：{cycle_code}/{dept_code}")
        return row

    def _ensure_open_snapshot(self, actor: dict, cycle_code: str,
                              dept_code: str) -> tuple[dict, dict, int]:
        """取（或创建）开放快照，并做角色/归属/周期状态校验。"""
        cycle = self._cycle_row(cycle_code)
        dept = self._department_by_code(dept_code)
        if actor["role"] == ROLE_DEPARTMENT and actor["department_id"] != dept["id"]:
            raise PermissionError_("只能提交本院系的材料")
        if actor["role"] not in (ROLE_PLANNING, ROLE_DEPARTMENT):
            raise PermissionError_("该角色不能提交材料")
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE cycle_id=? AND department_id=?",
            (cycle["id"], dept["id"]),
        ).fetchone()
        if row is None:
            if cycle["status"] != "open":
                raise ConflictError("周期已关闭，不能新建快照")
            cur = self.conn.execute(
                "INSERT INTO snapshots(cycle_id,department_id,status,"
                "created_by,created_at) VALUES(?,?,'open',?,?)",
                (cycle["id"], dept["id"], actor["id"],
                 self.clock.now().isoformat()),
            )
            row = self.conn.execute(
                "SELECT * FROM snapshots WHERE id=?", (cur.lastrowid,)).fetchone()
        elif row["status"] == "sealed":
            raise ConflictError("快照已按周期封存，不能再修改")
        return cycle, dept, row["id"]

    def put_item(self, actor: dict, cycle_code: str, dept_code: str,
                 kind: str, payload: dict) -> dict:
        if kind not in SNAPSHOT_KINDS:
            raise DomainError(f"材料类型无效：{kind}")
        validate_payload(kind, payload)
        with self.conn:
            cycle = self._cycle_row(cycle_code)
            if cycle["status"] != "open":
                raise ConflictError("周期已关闭，不能修改材料")
            _, _, snapshot_id = self._ensure_open_snapshot(
                actor, cycle_code, dept_code)
            now = self.clock.now().isoformat()
            self.conn.execute(
                "INSERT INTO snapshot_items(snapshot_id,kind,payload_json,"
                "updated_by,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(snapshot_id,kind) DO UPDATE SET "
                "payload_json=excluded.payload_json, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (snapshot_id, kind, canonical_json(payload), actor["id"], now),
            )
            self.audit.append(now, actor, "snapshot.item_put",
                              f"snapshot:{cycle_code}/{dept_code}/{kind}",
                              {"kind": kind, "payload_hash": digest(payload)})
        return self.get_snapshot(actor, cycle_code, dept_code)

    def _load_items(self, snapshot_id: int) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT kind,payload_json FROM snapshot_items WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        return {r["kind"]: json.loads(r["payload_json"]) for r in rows}

    def get_snapshot(self, actor: dict, cycle_code: str, dept_code: str) -> dict:
        snap = dict(self._snapshot_row(cycle_code, dept_code))
        if actor["role"] == ROLE_DEPARTMENT:
            dept = self._department_by_code(dept_code)
            if actor["department_id"] != dept["id"]:
                raise PermissionError_("只能查看本院系快照")
        items = {}
        for r in self.conn.execute(
            "SELECT kind,payload_json,updated_at FROM snapshot_items "
            "WHERE snapshot_id=? ORDER BY kind", (snap["id"],)
        ).fetchall():
            items[r["kind"]] = {"payload": json.loads(r["payload_json"]),
                                "updated_at": r["updated_at"]}
        cycle = self._cycle_row(cycle_code)
        dept = self._department_by_code(dept_code)
        snap["cycle_code"] = cycle["code"]
        snap["department_code"] = dept["code"]
        snap["department_name"] = dept["name"]
        snap["items"] = items
        snap["missing_kinds"] = [k for k in SNAPSHOT_KINDS if k not in items]
        snap.pop("cycle_id", None)
        snap.pop("department_id", None)
        return snap

    def list_snapshots(self, actor: dict, cycle_code: str | None = None) -> list[dict]:
        sql = ("SELECT s.id, c.code AS cycle_code, d.code AS department_code, "
               "d.name AS department_name, s.status, s.created_at, s.sealed_at, "
               "s.rule_set_version, s.chain_hash FROM snapshots s "
               "JOIN cycles c ON c.id=s.cycle_id "
               "JOIN departments d ON d.id=s.department_id")
        params: list[Any] = []
        if actor["role"] == ROLE_DEPARTMENT:
            sql += " WHERE s.department_id=?"
            params.append(actor["department_id"])
            if cycle_code:
                sql += " AND c.code=?"
                params.append(cycle_code)
        elif cycle_code:
            sql += " WHERE c.code=?"
            params.append(cycle_code)
        sql += " ORDER BY c.code, d.code"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def provisional_evaluation(self, actor: dict, cycle_code: str,
                               dept_code: str) -> dict:
        """封存前试算：明确标注 provisional，使用当前批准版本，不落任何案件。"""
        snap = self._snapshot_row(cycle_code, dept_code)
        if snap["status"] == "sealed":
            raise ConflictError("快照已封存，请使用重放接口")
        rs = self._current_approved_rule_set()
        items = self._load_items(snap["id"])
        missing = [k for k in SNAPSHOT_KINDS if k not in items]
        result: dict[str, Any] = {
            "provisional": True,
            "rule_set_version": rs["version"],
            "missing_kinds": missing,
        }
        if not missing:
            findings = evaluate(items, rs["rules"])
            result["findings"] = findings
            result["findings_hash"] = findings_hash(findings)
        else:
            result["findings"] = []
            result["note"] = "材料不完整，结果仅供参考"
        return result

    def seal_snapshot(self, actor: dict, cycle_code: str, dept_code: str) -> dict:
        """发展规划处封存：钉死规则版本、计算发现、生成待核案件，全部在一个事务内。"""
        self.require_role(actor, ROLE_PLANNING)
        with self.conn:
            cycle = self._cycle_row(cycle_code)
            if cycle["status"] != "open":
                raise ConflictError("周期已关闭，不能封存")
            dept = self._department_by_code(dept_code)
            snap = self._snapshot_row(cycle_code, dept_code)
            if snap["status"] == "sealed":
                raise ConflictError("快照已封存")
            items = self._load_items(snap["id"])
            missing = [k for k in SNAPSHOT_KINDS if k not in items]
            if missing:
                raise ConflictError("材料不完整，无法封存，缺少："
                                    + "、".join(missing))
            rs = self._current_approved_rule_set()
            sealed_at = self.clock.now()
            sealed_on = sealed_at.date()

            findings = evaluate(items, rs["rules"])
            fhash = findings_hash(findings)
            ihash = digest({k: items[k] for k in SNAPSHOT_KINDS})

            prev = self.conn.execute(
                "SELECT chain_hash FROM snapshots WHERE department_id=? "
                "AND status='sealed' ORDER BY sealed_at DESC, id DESC LIMIT 1",
                (dept["id"],),
            ).fetchone()
            prev_hash = prev["chain_hash"] if prev else None
            chain_body = {
                "cycle_code": cycle["code"],
                "department_code": dept["code"],
                "rule_set_version": rs["version"],
                "items_hash": ihash,
                "findings_hash": fhash,
                "sealed_on": sealed_on.isoformat(),
            }
            chain_hash = chain_digest(prev_hash, chain_body)

            self.conn.execute(
                "UPDATE snapshots SET status='sealed', sealed_by=?, sealed_at=?, "
                "rule_set_version=?, items_hash=?, findings_hash=?, prev_hash=?, "
                "chain_hash=? WHERE id=?",
                (actor["id"], sealed_at.isoformat(), rs["version"], ihash,
                 fhash, prev_hash, chain_hash, snap["id"]),
            )

            settings = rs["settings"]
            explanation_due = (
                sealed_on + timedelta(days=settings["explanation_days"])
            ).isoformat()
            existing = self.conn.execute(
                "SELECT COUNT(*) AS n FROM cases WHERE cycle_id=? AND department_id=?",
                (cycle["id"], dept["id"]),
            ).fetchone()["n"]

            created_cases = []
            for i, f in enumerate(findings):
                case_no = f"{cycle['code']}-{dept['code']}-{existing + i + 1:03d}"
                initial_state = STATE_MONITOR if f["level"] == "watch" else STATE_WARN
                exempt = self._active_exemption(dept["id"], f["rule_id"],
                                                f["subject"], sealed_on)
                if exempt:
                    status, close_reason, exempt_until = (
                        STATE_CLOSED,
                        f"豁免沿用至 {exempt['exempt_until']}",
                        exempt["exempt_until"],
                    )
                else:
                    status, close_reason, exempt_until = initial_state, None, None
                self.conn.execute(
                    "INSERT INTO cases(case_no,cycle_id,department_id,snapshot_id,"
                    "rule_set_version,rule_id,subject,level,status,title,"
                    "detail_json,findings_hash,explanation_due,exempt_until,"
                    "opened_at,created_by,closed_at,close_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (case_no, cycle["id"], dept["id"], snap["id"], rs["version"],
                     f["rule_id"], f["subject"], f["level"], status,
                     f["rule_name"], canonical_json(f), fhash, explanation_due,
                     exempt_until, sealed_at.isoformat(), actor["id"],
                     sealed_at.isoformat() if exempt else None, close_reason),
                )
                case_row = self.conn.execute(
                    "SELECT * FROM cases WHERE case_no=?", (case_no,)).fetchone()
                self._event(case_row["id"], sealed_at.isoformat(), actor,
                            "case.open",
                            {"state": status, "finding": f,
                             "explanation_due": None if exempt else explanation_due,
                             "exemption_applied": bool(exempt)})
                created_cases.append(case_no)

            self.audit.append(sealed_at.isoformat(), actor, "snapshot.seal",
                              f"snapshot:{cycle_code}/{dept_code}",
                              {**chain_body, "prev_hash": prev_hash,
                               "case_count": len(created_cases),
                               "case_numbers": created_cases})
        return self.get_snapshot(actor, cycle_code, dept_code)

    def _active_exemption(self, dept_id: int, rule_id: str, subject: str,
                          on_date: date) -> dict | None:
        """查同院系同规则同主题、豁免仍在有效期内的最近已关闭案件。"""
        row = self.conn.execute(
            "SELECT c.exempt_until FROM cases c "
            "JOIN snapshots s ON s.id=c.snapshot_id "
            "WHERE c.department_id=? AND c.rule_id=? AND c.subject=? "
            "AND c.close_reason LIKE '豁免%' AND c.exempt_until IS NOT NULL "
            "AND c.exempt_until >= ? ORDER BY c.exempt_until DESC, c.id DESC LIMIT 1",
            (dept_id, rule_id, subject, on_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    # ---------- 案件 ----------

    def _case_row(self, case_no: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cases WHERE case_no=?", (case_no,)).fetchone()
        if not row:
            raise NotFoundError(f"案件不存在：{case_no}")
        return row

    def _can_access_case(self, actor: dict, case: sqlite3.Row) -> None:
        if actor["role"] == ROLE_DEPARTMENT and actor["department_id"] != case["department_id"]:
            raise PermissionError_("只能访问本院系案件")

    def _case_view(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        dept = self.conn.execute("SELECT code,name FROM departments WHERE id=?",
                                 (row["department_id"],)).fetchone()
        cyc = self.conn.execute("SELECT code,name FROM cycles WHERE id=?",
                                (row["cycle_id"],)).fetchone()
        d["department_code"] = dept["code"]
        d["department_name"] = dept["name"]
        d["cycle_code"] = cyc["code"]
        d["cycle_name"] = cyc["name"]
        d["detail"] = json.loads(row["detail_json"])
        d.pop("detail_json", None)
        today = self.clock.today()
        d["explanation_overdue"] = (
            row["status"] in (STATE_MONITOR, STATE_WARN, STATE_VERIFY)
            and row["explanation_submitted_at"] is None
            and row["explanation_due"] is not None
            and today.isoformat() > row["explanation_due"]
        )
        d["rectification_overdue"] = (
            row["status"] == STATE_RECTIFY
            and row["rectification_submitted_at"] is None
            and row["rectification_due"] is not None
            and today.isoformat() > row["rectification_due"]
        )
        d["exemption_active"] = (
            row["exempt_until"] is not None
            and today.isoformat() <= row["exempt_until"]
        )
        d.pop("cycle_id", None)
        d.pop("department_id", None)
        d.pop("snapshot_id", None)
        return d

    def list_cases(self, actor: dict, cycle_code: str | None = None,
                   dept_code: str | None = None, status: str | None = None,
                   overdue_only: bool = False) -> list[dict]:
        sql = "SELECT c.* FROM cases c JOIN cycles cy ON cy.id=c.cycle_id JOIN departments d ON d.id=c.department_id WHERE 1=1"
        params: list[Any] = []
        if actor["role"] == ROLE_DEPARTMENT:
            sql += " AND c.department_id=?"
            params.append(actor["department_id"])
        elif dept_code:
            sql += " AND d.code=?"
            params.append(dept_code)
        if cycle_code:
            sql += " AND cy.code=?"
            params.append(cycle_code)
        if status:
            if status not in (STATE_MONITOR, STATE_WARN, STATE_VERIFY,
                              STATE_RECTIFY, STATE_CLOSED):
                raise DomainError("status 无效")
            sql += " AND c.status=?"
            params.append(status)
        sql += " ORDER BY cy.code, d.code, c.case_no"
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        views = [self._case_view(self._case_row(r["case_no"])) for r in rows]
        if overdue_only:
            views = [v for v in views
                     if v["explanation_overdue"] or v["rectification_overdue"]]
        return views

    def get_case(self, actor: dict, case_no: str) -> dict:
        case = self._case_row(case_no)
        self._can_access_case(actor, case)
        return self._case_view(case)

    def _event(self, case_id: int, at: str, actor: dict | None,
               event_type: str, detail: dict) -> None:
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()["s"]
        self.conn.execute(
            "INSERT INTO case_events(case_id,seq,at,actor_id,actor_name,role,"
            "event_type,detail_json) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, seq, at, actor["id"] if actor else None,
             actor["name"] if actor else "系统",
             ROLE_NAMES[actor["role"]] if actor else "系统",
             event_type, canonical_json(detail)),
        )

    def case_timeline(self, actor: dict, case_no: str) -> list[dict]:
        case = self._case_row(case_no)
        self._can_access_case(actor, case)
        return [
            {"seq": r["seq"], "at": r["at"], "actor_name": r["actor_name"],
             "role": r["role"], "event_type": r["event_type"],
             "detail": json.loads(r["detail_json"])}
            for r in self.conn.execute(
                "SELECT * FROM case_events WHERE case_id=? ORDER BY seq",
                (case["id"],)).fetchall()
        ]

    def _require_open_case(self, actor: dict, case_no: str) -> sqlite3.Row:
        case = self._case_row(case_no)
        self._can_access_case(actor, case)
        if case["status"] == STATE_CLOSED:
            raise ConflictError("案件已关闭")
        return case

    def submit_explanation(self, actor: dict, case_no: str,
                           text: str) -> dict:
        self.require_role(actor, ROLE_DEPARTMENT)
        if not text or not text.strip():
            raise DomainError("解释内容不能为空")
        with self.conn:
            case = self._require_open_case(actor, case_no)
            if case["status"] not in (STATE_MONITOR, STATE_WARN, STATE_VERIFY):
                raise ConflictError("案件已进入整改阶段，不能再提交解释")
            now = self.clock.now()
            overdue = bool(case["explanation_due"]
                           and now.date().isoformat() > case["explanation_due"])
            self.conn.execute(
                "UPDATE cases SET explanation_submitted_at=? WHERE id=?",
                (now.isoformat(), case["id"]),
            )
            self._event(case["id"], now.isoformat(), actor,
                        "explanation.submit",
                        {"text": text, "overdue": overdue,
                         "due": case["explanation_due"]})
            self.audit.append(now.isoformat(), actor, "case.explanation",
                              f"case:{case_no}", {"overdue": overdue})
        return self.get_case(actor, case_no)

    def start_verification(self, actor: dict, case_no: str,
                           note: str | None) -> dict:
        self.require_role(actor, ROLE_SUPERVISOR)
        with self.conn:
            case = self._require_open_case(actor, case_no)
            if case["status"] not in (STATE_MONITOR, STATE_WARN):
                raise ConflictError("只有监测/预警中的案件可以立案核实")
            now = self.clock.now()
            self.conn.execute("UPDATE cases SET status=? WHERE id=?",
                              (STATE_VERIFY, case["id"]))
            self._event(case["id"], now.isoformat(), actor,
                        "verification.start", {"note": note})
            self.audit.append(now.isoformat(), actor, "case.verify_start",
                              f"case:{case_no}", {"note": note})
        return self.get_case(actor, case_no)

    def decide_case(self, actor: dict, case_no: str, verdict: str,
                    note: str | None, exempt_until: str | None) -> dict:
        """督导裁定：unsubstantiated（不成立）/ exempt（限期豁免）/ rectify（限期整改）。

        预警本身不是违规定性：只有 rectify 裁定才进入整改。
        """
        self.require_role(actor, ROLE_SUPERVISOR)
        if verdict not in ("unsubstantiated", "exempt", "rectify"):
            raise DomainError("verdict 只能是 unsubstantiated/exempt/rectify")
        with self.conn:
            case = self._require_open_case(actor, case_no)
            if case["status"] not in (STATE_MONITOR, STATE_WARN, STATE_VERIFY):
                raise ConflictError("当前状态不能作出裁定")
            now = self.clock.now()
            rs = self.get_rule_set(case["rule_set_version"])
            max_days = rs["settings"]["exemption_max_days"]
            detail: dict[str, Any] = {"verdict": verdict, "note": note}

            if verdict == "unsubstantiated":
                self._close(case, now.isoformat(), actor, "不成立", detail)
            elif verdict == "exempt":
                if not exempt_until:
                    raise DomainError("豁免裁定必须给出 exempt_until")
                exempt_date = date.fromisoformat(_iso_d(exempt_until))
                latest = now.date() + timedelta(days=max_days)
                if exempt_date < now.date():
                    raise DomainError("豁免截止日不能早于今天")
                if exempt_date > latest:
                    raise DomainError(
                        f"豁免期限最长 {max_days} 天（不得晚于 {latest.isoformat()}）")
                self.conn.execute(
                    "UPDATE cases SET exempt_until=? WHERE id=?",
                    (exempt_date.isoformat(), case["id"]),
                )
                detail["exempt_until"] = exempt_date.isoformat()
                self._close(case, now.isoformat(), actor, "豁免", detail)
            else:
                due = (now.date() + timedelta(
                    days=rs["settings"]["rectification_days"])).isoformat()
                self.conn.execute(
                    "UPDATE cases SET status=?, rectification_due=? WHERE id=?",
                    (STATE_RECTIFY, due, case["id"]),
                )
                detail["rectification_due"] = due
                self._event(case["id"], now.isoformat(), actor,
                            "decision.rectify", detail)
            self.audit.append(now.isoformat(), actor, "case.decide",
                              f"case:{case_no}", detail)
        return self.get_case(actor, case_no)

    def _close(self, case: sqlite3.Row, at: str, actor: dict,
               reason: str, detail: dict) -> None:
        self.conn.execute(
            "UPDATE cases SET status=?, closed_at=?, close_reason=? WHERE id=?",
            (STATE_CLOSED, at, reason, case["id"]),
        )
        self._event(case["id"], at, actor, "case.close",
                    {**detail, "close_reason": reason})

    def submit_rectification(self, actor: dict, case_no: str,
                             text: str) -> dict:
        self.require_role(actor, ROLE_DEPARTMENT)
        if not text or not text.strip():
            raise DomainError("整改报告不能为空")
        with self.conn:
            case = self._require_open_case(actor, case_no)
            if case["status"] != STATE_RECTIFY:
                raise ConflictError("只有整改中的案件可以提交整改报告")
            now = self.clock.now()
            overdue = bool(case["rectification_due"]
                           and now.date().isoformat() > case["rectification_due"])
            self.conn.execute(
                "UPDATE cases SET rectification_submitted_at=? WHERE id=?",
                (now.isoformat(), case["id"]),
            )
            self._event(case["id"], now.isoformat(), actor,
                        "rectification.submit",
                        {"text": text, "overdue": overdue,
                         "due": case["rectification_due"]})
            self.audit.append(now.isoformat(), actor, "case.rectification",
                              f"case:{case_no}", {"overdue": overdue})
        return self.get_case(actor, case_no)

    def accept_rectification(self, actor: dict, case_no: str,
                             accepted: bool, note: str | None) -> dict:
        self.require_role(actor, ROLE_SUPERVISOR)
        with self.conn:
            case = self._require_open_case(actor, case_no)
            if case["status"] != STATE_RECTIFY:
                raise ConflictError("案件不在整改阶段")
            now = self.clock.now()
            if accepted:
                if not case["rectification_submitted_at"]:
                    raise ConflictError("院系尚未提交整改报告，不能验收通过")
                self._close(case, now.isoformat(), actor, "整改通过",
                            {"note": note})
            else:
                self._event(case["id"], now.isoformat(), actor,
                            "rectification.reject", {"note": note})
            self.audit.append(now.isoformat(), actor, "case.acceptance",
                              f"case:{case_no}", {"accepted": accepted,
                                                  "note": note})
        return self.get_case(actor, case_no)

    # ---------- 重放 ----------

    def replay_snapshot(self, cycle_code: str, dept_code: str) -> dict:
        """用快照钉死的规则版本重放：任何人、任何时间结果必须一致。"""
        snap = self._snapshot_row(cycle_code, dept_code)
        if snap["status"] != "sealed":
            raise ConflictError("快照尚未封存，没有可重放的结论")
        rs = self.get_rule_set(snap["rule_set_version"])
        items = self._load_items(snap["id"])
        findings = evaluate(items, rs["rules"])
        fhash = findings_hash(findings)
        ihash = digest({k: items[k] for k in SNAPSHOT_KINDS})
        chain_body = {
            "cycle_code": cycle_code,
            "department_code": dept_code,
            "rule_set_version": rs["version"],
            "items_hash": ihash,
            "findings_hash": fhash,
            "sealed_on": snap["sealed_at"][:10],
        }
        replay_chain = chain_digest(snap["prev_hash"], chain_body)
        stored_cases = [dict(r) for r in self.conn.execute(
            "SELECT case_no,rule_id,subject,level,status FROM cases "
            "WHERE snapshot_id=? ORDER BY case_no", (snap["id"],)).fetchall()]
        replayed_keys = [(f["rule_id"], f["subject"], f["level"]) for f in findings]
        stored_keys = [(c["rule_id"], c["subject"], c["level"]) for c in stored_cases]
        return {
            "snapshot": f"{cycle_code}/{dept_code}",
            "rule_set_version": rs["version"],
            "items_hash_match": ihash == snap["items_hash"],
            "findings_hash_match": fhash == snap["findings_hash"],
            "chain_hash_match": replay_chain == snap["chain_hash"],
            "stored_items_hash": snap["items_hash"],
            "replayed_items_hash": ihash,
            "stored_findings_hash": snap["findings_hash"],
            "replayed_findings_hash": fhash,
            "findings": findings,
            "cases_match": replayed_keys == stored_keys,
            "stored_case_count": len(stored_cases),
        }

    def replay_cycle(self, cycle_code: str) -> dict:
        cycle = self._cycle_row(cycle_code)
        rows = self.conn.execute(
            "SELECT d.code FROM snapshots s JOIN departments d ON d.id=s.department_id "
            "WHERE s.cycle_id=? AND s.status='sealed' ORDER BY d.code",
            (cycle["id"],)).fetchall()
        reports = [self.replay_snapshot(cycle_code, r["code"]) for r in rows]
        return {
            "cycle": cycle_code,
            "snapshot_count": len(reports),
            "all_match": all(
                r["items_hash_match"] and r["findings_hash_match"]
                and r["chain_hash_match"] and r["cases_match"] for r in reports
            ),
            "snapshots": reports,
        }
