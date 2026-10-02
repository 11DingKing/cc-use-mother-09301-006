"""应用服务层：把领域规则编排为用例。

角色（与 domain/contract.json 一致）：

* 发展规划处：建周期、收快照、封存、起草与审批规则、发起评估。
* 院系负责人：提交解释、申请豁免、提交整改。
* 校级督导：授予/撤销豁免、核实案件、验收整改。

关键原则：

* 评估是纯函数，结果只取决于运行清单（快照哈希 + 规则快照 +
  豁免快照 + 引擎版本）；运行记录只增不改。
* 预警案件不是违规定性：``alert`` 仅表示"待核"，必须经解释、
  核实才可能进入整改。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .clock import Clock, parse_ts
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .hashing import canonical, digest
from .rules_dsl import ENGINE_VERSION, RuleError, evaluate, validate_definition
from .storage import connect, init_db, insert, query_all, query_one

ROLE_PLANNER = "发展规划处"
ROLE_UNIT = "院系负责人"
ROLE_SUPERVISOR = "校级督导"

SNAPSHOT_KINDS = ("mission", "commitment", "discipline_input", "enrollment", "service_output")
SNAPSHOT_KIND_LABELS = {
    "mission": "使命",
    "commitment": "承诺目标",
    "discipline_input": "学科投入",
    "enrollment": "招生结构",
    "service_output": "服务成果",
}
OPEN_STATUSES = ("alert", "verifying", "rectifying")


@dataclass
class Actor:
    role: str
    name: str
    unit_id: str | None = None

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise Forbidden(f"需要角色：{'、'.join(roles)}（当前：{self.role}）")

    def as_json(self) -> str:
        return f"{self.role}:{self.name}"


class App:
    def __init__(
        self,
        db_path: str = ":memory:",
        clock: Clock | None = None,
        explain_days: int = 10,
        exempt_days: int = 15,
        rectify_days: int = 30,
    ) -> None:
        self.conn: sqlite3.Connection = connect(db_path)
        init_db(self.conn)
        self.clock = clock or Clock()
        self.explain_days = explain_days
        self.exempt_days = exempt_days
        self.rectify_days = rectify_days

    # ---------- 基础档案 ----------

    def create_unit(self, actor: Actor, unit_id: str, name: str) -> dict:
        actor.require(ROLE_PLANNER)
        insert(self.conn, "units", {"id": unit_id, "name": name, "created_at": self.clock.now()})
        self.conn.commit()
        return {"id": unit_id, "name": name}

    def create_cycle(self, actor: Actor, cycle_id: str, label: str) -> dict:
        actor.require(ROLE_PLANNER)
        row = query_one(self.conn, "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM cycles")
        insert(self.conn, "cycles", {
            "id": cycle_id, "ordinal": row["n"], "label": label, "created_at": self.clock.now(),
        })
        self.conn.commit()
        return {"id": cycle_id, "ordinal": row["n"], "label": label}

    # ---------- 快照 ----------

    def submit_snapshot(self, actor: Actor, cycle_id: str, unit_id: str, kind: str, payload: dict) -> dict:
        actor.require(ROLE_PLANNER, ROLE_UNIT)
        if kind not in SNAPSHOT_KINDS:
            raise ValidationError(f"快照类型非法：{kind}")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("快照内容必须是非空对象")
        cycle = self._cycle(cycle_id)
        self._unit(unit_id)
        if cycle["sealed"]:
            raise Conflict("周期已封存，不能再提交快照")
        existing = query_one(
            self.conn,
            "SELECT id FROM snapshots WHERE cycle_id=? AND unit_id=? AND kind=?",
            (cycle_id, unit_id, kind),
        )
        if existing:
            # 封存前允许修订（此时尚不构成证据），封存后由数据库触发器硬阻止
            try:
                self.conn.execute(
                    "UPDATE snapshots SET payload=?, submitted_by=?, submitted_at=? WHERE id=?",
                    (canonical(payload), actor.as_json(), self.clock.now(), existing["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("快照已封存，禁止修改") from exc
            snapshot_id = existing["id"]
        else:
            snapshot_id = uuid.uuid4().hex
            insert(self.conn, "snapshots", {
                "id": snapshot_id, "cycle_id": cycle_id, "unit_id": unit_id, "kind": kind,
                "payload": canonical(payload), "submitted_by": actor.as_json(),
                "submitted_at": self.clock.now(),
            })
        self.conn.commit()
        return {"id": snapshot_id, "cycle_id": cycle_id, "unit_id": unit_id, "kind": kind, "sealed": False}

    def seal_cycle(self, actor: Actor, cycle_id: str) -> dict:
        """封存周期：五要素齐全后逐份计算哈希并冻结。"""
        actor.require(ROLE_PLANNER)
        cycle = self._cycle(cycle_id)
        if cycle["sealed"]:
            raise Conflict("周期已封存")
        units = query_all(self.conn, "SELECT id FROM units ORDER BY id")
        if not units:
            raise Conflict("尚无院系档案，不能封存")
        missing: list[str] = []
        for unit in units:
            for kind in SNAPSHOT_KINDS:
                row = query_one(
                    self.conn,
                    "SELECT 1 FROM snapshots WHERE cycle_id=? AND unit_id=? AND kind=?",
                    (cycle_id, unit["id"], kind),
                )
                if row is None:
                    missing.append(f"{unit['id']}/{SNAPSHOT_KIND_LABELS[kind]}")
        if missing:
            raise Conflict("五要素未齐，不能封存；缺少：" + "、".join(missing))
        now = self.clock.now()
        rows = query_all(
            self.conn,
            "SELECT id, cycle_id, unit_id, kind, payload FROM snapshots WHERE cycle_id=?",
            (cycle_id,),
        )
        for row in rows:
            h = digest({
                "cycle_id": row["cycle_id"], "unit_id": row["unit_id"],
                "kind": row["kind"], "payload": json.loads(row["payload"]),
            })
            self.conn.execute("UPDATE snapshots SET sealed_at=?, hash=? WHERE id=?", (now, h, row["id"]))
        self.conn.execute("UPDATE cycles SET sealed=1, sealed_at=? WHERE id=?", (now, cycle_id))
        self.conn.commit()
        return {"cycle_id": cycle_id, "sealed_at": now, "snapshot_count": len(rows)}

    def get_snapshot(self, cycle_id: str, unit_id: str, kind: str) -> dict:
        row = query_one(
            self.conn,
            "SELECT * FROM snapshots WHERE cycle_id=? AND unit_id=? AND kind=?",
            (cycle_id, unit_id, kind),
        )
        if row is None:
            raise NotFound("快照不存在")
        return _snapshot_dict(row)

    def list_cycles(self) -> list[dict]:
        return [dict(r) for r in query_all(self.conn, "SELECT * FROM cycles ORDER BY ordinal")]

    # ---------- 规则：起草、审批、退役（只增不改） ----------

    def create_rule_draft(self, actor: Actor, name: str, definition: dict, description: str = "") -> dict:
        actor.require(ROLE_PLANNER)
        validate_definition(definition)
        rid = definition["id"]
        if query_one(self.conn, "SELECT 1 FROM rules WHERE id=?", (rid,)):
            raise Conflict(f"规则标识已存在：{rid}")
        version = query_one(self.conn, "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM rules")["n"]
        insert(self.conn, "rules", {
            "id": rid, "version": version, "name": name, "description": description,
            "definition": canonical(definition), "status": "draft", "created_at": self.clock.now(),
        })
        self.conn.commit()
        return {"id": rid, "version": version, "status": "draft"}

    def approve_rule(self, actor: Actor, rule_id: str, effective_from_cycle: str) -> dict:
        """批准规则并指定生效周期；生效周期必须晚于最近已封存周期。"""
        actor.require(ROLE_PLANNER)
        rule = self._rule(rule_id)
        if rule["status"] != "draft":
            raise Conflict("只有草案规则可以批准")
        eff = self._cycle(effective_from_cycle)
        latest_sealed = query_one(
            self.conn, "SELECT COALESCE(MAX(ordinal), -1) AS o FROM cycles WHERE sealed=1"
        )["o"]
        if eff["ordinal"] <= latest_sealed:
            raise Conflict("规则升级只能对后续周期生效，生效周期必须晚于最近已封存周期")
        self.conn.execute(
            "UPDATE rules SET status='approved', effective_from_cycle=?, approved_by=?, approved_at=? WHERE id=?",
            (effective_from_cycle, actor.as_json(), self.clock.now(), rule_id),
        )
        self.conn.commit()
        return {"id": rule_id, "status": "approved", "effective_from_cycle": effective_from_cycle}

    def retire_rule(self, actor: Actor, rule_id: str, retired_from_cycle: str) -> dict:
        """退役规则，且只对指定的未来周期起生效；旧周期重放仍按旧规则。"""
        actor.require(ROLE_PLANNER)
        rule = self._rule(rule_id)
        if rule["status"] != "approved":
            raise Conflict("只有生效中的规则可以退役")
        retire = self._cycle(retired_from_cycle)
        latest_sealed = query_one(
            self.conn, "SELECT COALESCE(MAX(ordinal), -1) AS o FROM cycles WHERE sealed=1"
        )["o"]
        if retire["ordinal"] <= latest_sealed:
            raise Conflict("退役只能对后续周期生效，周期必须晚于最近已封存周期")
        self.conn.execute(
            "UPDATE rules SET status='retired', retired_from_cycle=? WHERE id=?",
            (retired_from_cycle, rule_id),
        )
        self.conn.commit()
        return {"id": rule_id, "status": "retired", "retired_from_cycle": retired_from_cycle}

    def list_rules(self, status: str | None = None) -> list[dict]:
        if status:
            rows = query_all(self.conn, "SELECT * FROM rules WHERE status=? ORDER BY version", (status,))
        else:
            rows = query_all(self.conn, "SELECT * FROM rules ORDER BY version")
        return [_rule_dict(r) for r in rows]

    # ---------- 豁免（校级督导授权，带周期边界） ----------

    def grant_exemption(
        self, actor: Actor, unit_id: str, cycle_from: str, cycle_to: str,
        reason: str, rule_id: str | None = None,
    ) -> dict:
        actor.require(ROLE_SUPERVISOR)
        self._unit(unit_id)
        if rule_id is not None:
            self._rule(rule_id)
        c_from, c_to = self._cycle(cycle_from), self._cycle(cycle_to)
        if c_from["ordinal"] > c_to["ordinal"]:
            raise ValidationError("豁免起始周期不能晚于结束周期")
        latest_sealed = query_one(
            self.conn, "SELECT COALESCE(MAX(ordinal), -1) AS o FROM cycles WHERE sealed=1"
        )["o"]
        if c_from["ordinal"] <= latest_sealed:
            raise Conflict("豁免只能对尚未封存的后续周期授予，不能回溯改变已封存周期")
        if not reason.strip():
            raise ValidationError("豁免理由不能为空")
        eid = uuid.uuid4().hex
        insert(self.conn, "exemptions", {
            "id": eid, "unit_id": unit_id, "rule_id": rule_id,
            "cycle_from": cycle_from, "cycle_to": cycle_to, "reason": reason,
            "granted_by": actor.as_json(), "granted_at": self.clock.now(),
        })
        self.conn.commit()
        return {"id": eid, "status": "active"}

    def revoke_exemption(self, actor: Actor, exemption_id: str, revoke_from_cycle: str) -> dict:
        """撤销豁免，只对指定的未来周期生效；旧周期重放仍视为豁免有效。"""
        actor.require(ROLE_SUPERVISOR)
        row = query_one(self.conn, "SELECT * FROM exemptions WHERE id=?", (exemption_id,))
        if row is None:
            raise NotFound("豁免不存在")
        if row["status"] != "active":
            raise Conflict("豁免已被撤销")
        revoke = self._cycle(revoke_from_cycle)
        latest_sealed = query_one(
            self.conn, "SELECT COALESCE(MAX(ordinal), -1) AS o FROM cycles WHERE sealed=1"
        )["o"]
        if revoke["ordinal"] <= latest_sealed:
            raise Conflict("撤销只能对后续周期生效，周期必须晚于最近已封存周期")
        self.conn.execute(
            "UPDATE exemptions SET status='revoked', revoked_by=?, revoked_at=?, revoke_from_cycle=? WHERE id=?",
            (actor.as_json(), self.clock.now(), revoke_from_cycle, exemption_id),
        )
        self.conn.commit()
        return {"id": exemption_id, "status": "revoked", "revoke_from_cycle": revoke_from_cycle}

    # ---------- 周期评估与重放 ----------

    def run_cycle(self, actor: Actor, cycle_id: str, *, replay: bool = False) -> dict:
        actor.require(ROLE_PLANNER, ROLE_SUPERVISOR)
        cycle = self._cycle(cycle_id)
        if not cycle["sealed"]:
            raise Conflict("只能评估已封存周期")
        kind = "replay" if replay else "official"
        if kind == "official" and query_one(
            self.conn, "SELECT 1 FROM runs WHERE cycle_id=? AND kind='official'", (cycle_id,)
        ):
            raise Conflict("该周期已有正式评估；如需核对请使用重放，结果不会改变案件")

        parent_run = None
        if kind == "replay":
            official = query_one(
                self.conn, "SELECT * FROM runs WHERE cycle_id=? AND kind='official'", (cycle_id,)
            )
            if official is None:
                raise Conflict("该周期尚无正式评估可比对")
            parent_run = official["id"]

        bundle = self._build_input_bundle(cycle)
        started = self.clock.now()
        run_id = uuid.uuid4().hex
        findings: list[dict] = []
        for unit_id in bundle["unit_order"]:
            # 严格按周期序数对齐：缺失周期给空字典，使基线/趋势引用解析为缺失，
            # 绝不跳过周期把更早的数据误当"上一周期"
            history = [bundle["merged"][cid].get(unit_id, {}) for cid in bundle["cycle_order"]]
            current = bundle["merged"][cycle_id][unit_id]
            for rule in bundle["rules"]:
                if self._is_exempt(bundle["exemptions"], unit_id, rule["id"]):
                    continue
                definition = json.loads(rule["definition"])
                try:
                    hit = evaluate(definition, current, history)
                except RuleError as exc:
                    raise ValidationError(f"规则 {rule['id']} 执行失败：{exc.message}") from exc
                if hit is None:
                    continue
                findings.append(self._make_finding(run_id, cycle_id, unit_id, rule, hit))

        finished = self.clock.now()
        result_hash = digest({
            "engine": ENGINE_VERSION,
            "ruleset": bundle["ruleset_hash"],
            "exemptions": bundle["exemptions_hash"],
            "inputs": bundle["input_hashes"],
            "findings": sorted(f["finding_hash"] for f in findings),
        })
        insert(self.conn, "runs", {
            "id": run_id, "cycle_id": cycle_id, "kind": kind,
            "triggered_by": actor.as_json(), "started_at": started, "finished_at": finished,
            "engine_version": ENGINE_VERSION,
            "ruleset_hash": bundle["ruleset_hash"], "exemptions_hash": bundle["exemptions_hash"],
            "result_hash": result_hash, "input_manifest": canonical(bundle["manifest"]),
            "parent_run_id": parent_run,
        })
        for finding in findings:
            insert(self.conn, "run_findings", finding)
        self.conn.commit()

        report = {
            "run_id": run_id, "cycle_id": cycle_id, "kind": kind,
            "engine_version": ENGINE_VERSION,
            "ruleset_hash": bundle["ruleset_hash"], "exemptions_hash": bundle["exemptions_hash"],
            "result_hash": result_hash, "finding_count": len(findings), "findings": findings,
        }
        if kind == "official":
            actions = self._upsert_cases(actor, cycle, run_id, findings)
            report["case_actions"] = actions
            return report
        official = query_one(
            self.conn, "SELECT result_hash FROM runs WHERE cycle_id=? AND kind='official'", (cycle_id,)
        )
        report["identical_to_official"] = (official["result_hash"] == result_hash)
        report["official_result_hash"] = official["result_hash"]
        return report

    def _build_input_bundle(self, cycle: sqlite3.Row) -> dict:
        """冻结一次评估的全部输入：规则集、豁免集、五要素快照。"""
        rules = query_all(
            self.conn,
            "SELECT * FROM rules WHERE status IN ('approved','retired') "
            "AND effective_from_cycle IN (SELECT id FROM cycles WHERE ordinal <= ?) "
            "AND (retired_from_cycle IS NULL OR retired_from_cycle IN "
            "     (SELECT id FROM cycles WHERE ordinal > ?)) "
            "ORDER BY version, id",
            (cycle["ordinal"], cycle["ordinal"]),
        )
        rules_snapshot = [
            {"id": r["id"], "version": r["version"], "definition": json.loads(r["definition"])}
            for r in rules
        ]
        ruleset_hash = digest({"engine": ENGINE_VERSION, "rules": rules_snapshot})

        ex_rows = query_all(
            self.conn,
            "SELECT ex.* FROM exemptions ex JOIN cycles c1 ON ex.cycle_from=c1.id "
            "JOIN cycles c2 ON ex.cycle_to=c2.id "
            "WHERE c1.ordinal<=? AND c2.ordinal>=? "
            "AND (ex.status='active' OR ex.revoke_from_cycle IN "
            "     (SELECT id FROM cycles WHERE ordinal > ?)) "
            "ORDER BY ex.id",
            (cycle["ordinal"], cycle["ordinal"], cycle["ordinal"]),
        )
        ex_snapshot = [{
            "id": r["id"], "unit_id": r["unit_id"], "rule_id": r["rule_id"],
            "cycle_from": r["cycle_from"], "cycle_to": r["cycle_to"], "reason": r["reason"],
        } for r in ex_rows]
        exemptions_hash = digest(ex_snapshot)

        cycles_rows = query_all(
            self.conn, "SELECT id FROM cycles WHERE sealed=1 AND ordinal<=? ORDER BY ordinal",
            (cycle["ordinal"],),
        )
        cycle_order = [r["id"] for r in cycles_rows]
        snap_rows = query_all(
            self.conn,
            "SELECT * FROM snapshots WHERE sealed_at IS NOT NULL AND cycle_id IN (%s) ORDER BY unit_id, kind"
            % ",".join("?" for _ in cycle_order),
            cycle_order,
        )
        merged: dict[str, dict[str, dict]] = {cid: {} for cid in cycle_order}
        input_hashes: dict[str, dict[str, str]] = {}
        unit_ids: set[str] = set()
        for row in snap_rows:
            payload = json.loads(row["payload"])
            # 主动重算封存哈希：即使有人绕过触发器直接改库文件，重放也会在此失配
            recomputed = digest({
                "cycle_id": row["cycle_id"], "unit_id": row["unit_id"],
                "kind": row["kind"], "payload": payload,
            })
            if recomputed != row["hash"]:
                raise Conflict(
                    f"封存快照哈希失配（{row['cycle_id']}/{row['unit_id']}/{row['kind']}）："
                    "快照可能在封存后被篡改，评估中止")
            merged[row["cycle_id"]].setdefault(row["unit_id"], {})[row["kind"]] = payload
            input_hashes.setdefault(row["unit_id"], {})[f"{row['cycle_id']}:{row['kind']}"] = row["hash"]
            unit_ids.add(row["unit_id"])
        # 仅对当前周期五要素齐全的单位出警；历史周期允许缺项（趋势序列相应缩短）
        unit_order = sorted(
            uid for uid in unit_ids
            if set(merged[cycle["id"]].get(uid, {})) == set(SNAPSHOT_KINDS)
        )
        manifest = {
            "cycle_id": cycle["id"], "rules": rules_snapshot, "exemptions": ex_snapshot,
            "inputs": input_hashes,
        }
        return {
            "rules": rules, "ruleset_hash": ruleset_hash,
            "exemptions": ex_snapshot, "exemptions_hash": exemptions_hash,
            "cycle_order": cycle_order, "merged": merged,
            "input_hashes": input_hashes, "unit_order": unit_order, "manifest": manifest,
        }

    @staticmethod
    def _is_exempt(exemptions: list[dict], unit_id: str, rule_id: str) -> bool:
        return any(
            e["unit_id"] == unit_id and (e["rule_id"] is None or e["rule_id"] == rule_id)
            for e in exemptions
        )

    def _make_finding(self, run_id: str, cycle_id: str, unit_id: str, rule: sqlite3.Row, hit: dict) -> dict:
        observed = {k: hit["values"][k] for k in sorted(hit["values"])}
        threshold = self._threshold_from_rule(json.loads(rule["definition"]))
        body = {
            "cycle_id": cycle_id, "unit_id": unit_id,
            "rule_id": rule["id"], "rule_version": rule["version"],
            "severity": hit["severity"], "value_path": hit["value_path"],
            "observed": observed, "threshold": threshold, "detail": hit["message"],
        }
        return {
            "id": uuid.uuid4().hex, "run_id": run_id, "unit_id": unit_id,
            "rule_id": rule["id"], "rule_version": rule["version"],
            "severity": hit["severity"], "value_path": hit["value_path"],
            "observed": canonical(observed), "threshold": canonical(threshold),
            "detail": hit["message"], "finding_hash": digest(body),
        }

    @staticmethod
    def _threshold_from_rule(definition: dict) -> dict:
        """从条件中抽取人可读的阈值（仅用于展示，不参与判定）。"""
        cond = definition.get("condition", {})
        if not isinstance(cond, dict):
            return {}
        (op, args), = cond.items()
        if op in ("and", "or"):
            for sub in args:
                found = App._threshold_from_rule(sub)
                if found:
                    return found
            return {}
        if op in (">", ">=", "<", "<=", "==", "!=", "between") and isinstance(args, list):
            return {"op": op, "params": args[1:]}
        return {}

    # ---------- 案件：预警不等于定性 ----------

    def _upsert_cases(self, actor: Actor, cycle: sqlite3.Row, run_id: str, findings: list[dict]) -> dict:
        created, reconfirmed = [], []
        for finding in findings:
            existing = query_one(
                self.conn,
                "SELECT * FROM cases WHERE unit_id=? AND rule_id=? AND status IN ('alert','verifying','rectifying')",
                (finding["unit_id"], finding["rule_id"]),
            )
            if existing:
                self._mutate_case(existing, actor, "reconfirmed", {"latest_run_id": run_id}, {
                    "run_id": run_id, "finding_hash": finding["finding_hash"],
                })
                reconfirmed.append(existing["id"])
            else:
                case_id = uuid.uuid4().hex
                now = self.clock.now()
                frozen = {
                    "cycle_id": cycle["id"], "unit_id": finding["unit_id"],
                    "rule_id": finding["rule_id"], "rule_version": finding["rule_version"],
                    "severity": finding["severity"], "value_path": finding["value_path"],
                    "observed": json.loads(finding["observed"]),
                    "threshold": json.loads(finding["threshold"]),
                    "detail": finding["detail"], "finding_hash": finding["finding_hash"],
                }
                row = {
                    "id": case_id, "unit_id": finding["unit_id"], "cycle_id": cycle["id"],
                    "rule_id": finding["rule_id"], "rule_version": finding["rule_version"],
                    "status": "alert", "finding_snapshot": canonical(frozen),
                    "finding_hash": finding["finding_hash"],
                    "first_run_id": run_id, "latest_run_id": run_id,
                    "explain_deadline": self.clock.iso_days_from_now(self.explain_days),
                    "exempt_deadline": self.clock.iso_days_from_now(self.exempt_days),
                    "rectify_deadline": None, "closed_reason": None,
                    "created_at": now, "updated_at": now,
                }
                row["prev_hash"] = None
                row["hash"] = digest(_case_hash_body(row))
                insert(self.conn, "cases", row)
                self._audit(case_id, "opened", actor, {
                    "run_id": run_id, "finding_hash": finding["finding_hash"],
                    "status": "alert", "note": "预警为待核线索，不构成违规定性",
                }, case_hash=row["hash"])
                created.append(case_id)
        self.conn.commit()
        return {"opened": created, "reconfirmed": reconfirmed}

    def submit_explanation(self, actor: Actor, case_id: str, text: str) -> dict:
        actor.require(ROLE_UNIT)
        case = self._open_case(case_id)
        self._require_unit(actor, case)
        if case["status"] != "alert":
            raise Conflict("仅预警状态的案件可以提交解释")
        self._check_deadline(case["explain_deadline"], "解释期限")
        if not text.strip():
            raise ValidationError("解释内容不能为空")
        self._mutate_case(case, actor, "explain_submitted", {
            "status": "verifying", "explain_text": text,
            "explain_submitted_at": self.clock.now(),
        }, {"text": text})
        return {"id": case_id, "status": "verifying"}

    def request_exemption(self, actor: Actor, case_id: str, reason: str) -> dict:
        actor.require(ROLE_UNIT)
        case = self._open_case(case_id)
        self._require_unit(actor, case)
        if case["status"] not in ("alert", "verifying"):
            raise Conflict("当前状态不能申请豁免")
        self._check_deadline(case["exempt_deadline"], "豁免申请期限")
        if not reason.strip():
            raise ValidationError("豁免申请理由不能为空")
        self._mutate_case(case, actor, "exempt_requested", {"exempt_status": "requested"}, {"reason": reason})
        return {"id": case_id, "status": case["status"], "exempt_status": "requested"}

    def decide_exemption(self, actor: Actor, case_id: str, granted: bool, opinion: str) -> dict:
        actor.require(ROLE_SUPERVISOR)
        case = self._open_case(case_id)
        if case["exempt_status"] != "requested":
            raise Conflict("没有待决的豁免申请")
        if granted:
            self._mutate_case(case, actor, "exempt_granted", {
                "status": "closed", "exempt_status": "granted",
                "closed_reason": f"豁免成立：{opinion}",
            }, {"granted": True, "opinion": opinion}, close=True)
            return {"id": case_id, "status": "closed"}
        self._mutate_case(case, actor, "exempt_denied", {
            "exempt_status": "denied",
        }, {"granted": False, "opinion": opinion})
        return {"id": case_id, "status": case["status"], "exempt_status": "denied"}

    def verify_case(self, actor: Actor, case_id: str, confirmed: bool, opinion: str) -> dict:
        """校级督导核实：不成立则关闭，成立则限期整改。"""
        actor.require(ROLE_SUPERVISOR)
        case = self._open_case(case_id)
        if case["status"] not in ("alert", "verifying"):
            raise Conflict("仅预警/核实中的案件可以作出核实结论")
        if not confirmed:
            self._mutate_case(case, actor, "verified_unsubstantiated", {
                "status": "closed", "closed_reason": f"核实不成立：{opinion}",
            }, {"confirmed": False, "opinion": opinion}, close=True)
            return {"id": case_id, "status": "closed"}
        self._mutate_case(case, actor, "verified_confirmed", {
            "status": "rectifying",
            "rectify_deadline": self.clock.iso_days_from_now(self.rectify_days),
        }, {"confirmed": True, "opinion": opinion})
        refreshed = query_one(self.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
        return {"id": case_id, "status": "rectifying", "rectify_deadline": refreshed["rectify_deadline"]}

    def submit_rectification(self, actor: Actor, case_id: str, plan: str) -> dict:
        actor.require(ROLE_UNIT)
        case = self._open_case(case_id)
        self._require_unit(actor, case)
        if case["status"] != "rectifying":
            raise Conflict("仅整改中的案件可以提交整改材料")
        self._check_deadline(case["rectify_deadline"], "整改期限")
        if not plan.strip():
            raise ValidationError("整改方案不能为空")
        self._mutate_case(case, actor, "rectify_submitted", {
            "rectify_plan": plan, "rectify_submitted_at": self.clock.now(),
        }, {"plan": plan})
        return {"id": case_id, "status": "rectifying"}

    def review_rectification(self, actor: Actor, case_id: str, accepted: bool, opinion: str,
                             extend_days: int | None = None) -> dict:
        actor.require(ROLE_SUPERVISOR)
        case = self._open_case(case_id)
        if case["status"] != "rectifying" or not case["rectify_plan"]:
            raise Conflict("尚无整改材料可供验收")
        if accepted:
            self._mutate_case(case, actor, "rectify_accepted", {
                "status": "closed", "closed_reason": f"整改验收通过：{opinion}",
            }, {"accepted": True, "opinion": opinion}, close=True)
            return {"id": case_id, "status": "closed"}
        updates: dict[str, Any] = {}
        if extend_days is not None:
            updates["rectify_deadline"] = self.clock.iso_days_from_now(extend_days)
        self._mutate_case(case, actor, "rectify_rejected", updates,
                          {"accepted": False, "opinion": opinion, "extend_days": extend_days})
        return {"id": case_id, "status": "rectifying"}

    def extend_deadline(self, actor: Actor, case_id: str, kind: str, days: int, reason: str) -> dict:
        """校级督导对逾期/将逾期的期限做延期，全程留痕。"""
        actor.require(ROLE_SUPERVISOR)
        if kind not in ("explain", "exempt", "rectify"):
            raise ValidationError("期限类型只能是 explain/exempt/rectify")
        if not isinstance(days, int) or days <= 0:
            raise ValidationError("延期天数必须是正整数")
        case = self._open_case(case_id)
        column = {"explain": "explain_deadline", "exempt": "exempt_deadline",
                  "rectify": "rectify_deadline"}[kind]
        if case[column] is None:
            raise Conflict(f"该案件当前没有 {kind} 期限（可能尚未进入对应阶段）")
        new_deadline = self.clock.iso_days_from_now(days)
        self._mutate_case(case, actor, "deadline_extended", {column: new_deadline},
                          {"kind": kind, "days": days, "reason": reason})
        return {"id": case_id, "kind": kind, "new_deadline": new_deadline}

    def list_cases(self, status: str | None = None, unit_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM cases WHERE 1=1", []
        if status:
            sql += " AND status=?"; params.append(status)
        if unit_id:
            sql += " AND unit_id=?"; params.append(unit_id)
        sql += " ORDER BY created_at, id"
        rows = query_all(self.conn, sql, params)
        return [_case_dict(r, self.clock.now()) for r in rows]

    def get_case(self, case_id: str) -> dict:
        row = query_one(self.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
        if row is None:
            raise NotFound("案件不存在")
        return _case_dict(row, self.clock.now())

    def case_timeline(self, case_id: str) -> dict:
        case = self.get_case(case_id)
        rows = query_all(self.conn, "SELECT * FROM case_audit WHERE case_id=? ORDER BY seq", (case_id,))
        return {"case": case, "timeline": [_audit_dict(r) for r in rows]}

    def verify_chain(self, case_id: str) -> dict:
        """重算案件与审计哈希链，发现任何篡改即报错。"""
        row = query_one(self.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
        if row is None:
            raise NotFound("案件不存在")
        if row["hash"] != digest(_case_hash_body(row)):
            raise Conflict("案件哈希链断裂：主记录被篡改")
        prev = None
        for audit in query_all(self.conn, "SELECT * FROM case_audit WHERE case_id=? ORDER BY seq", (case_id,)):
            if audit["prev_hash"] != prev:
                raise Conflict(f"审计链断裂：序号 {audit['seq']} 的前驱不匹配")
            body = {"case_id": audit["case_id"], "seq": audit["seq"], "action": audit["action"],
                    "actor": audit["actor"], "at": audit["at"], "payload": json.loads(audit["payload"])}
            if audit["hash"] != digest(body):
                raise Conflict(f"审计链断裂：序号 {audit['seq']} 内容被篡改")
            prev = audit["hash"]
        return {"case_id": case_id, "ok": True, "audit_count": audit["seq"]}

    def get_run(self, run_id: str) -> dict:
        row = query_one(self.conn, "SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise NotFound("评估运行不存在")
        findings = query_all(self.conn, "SELECT * FROM run_findings WHERE run_id=? ORDER BY unit_id, rule_id",
                             (run_id,))
        return {
            "id": row["id"], "cycle_id": row["cycle_id"], "kind": row["kind"],
            "triggered_by": row["triggered_by"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "engine_version": row["engine_version"],
            "ruleset_hash": row["ruleset_hash"], "exemptions_hash": row["exemptions_hash"],
            "result_hash": row["result_hash"], "parent_run_id": row["parent_run_id"],
            "input_manifest": json.loads(row["input_manifest"]),
            "findings": [{
                "unit_id": f["unit_id"], "rule_id": f["rule_id"], "rule_version": f["rule_version"],
                "severity": f["severity"], "value_path": f["value_path"],
                "observed": json.loads(f["observed"]), "threshold": json.loads(f["threshold"]),
                "detail": f["detail"], "finding_hash": f["finding_hash"],
            } for f in findings],
        }

    # ---------- 内部工具 ----------

    def _mutate_case(self, case: sqlite3.Row, actor: Actor, action: str,
                     updates: dict, audit_payload: dict, *, close: bool = False) -> None:
        body = dict(_case_hash_body(case))
        body.update(updates)
        new_hash = digest(body)
        now = self.clock.now()
        sets = ", ".join(f"{k}=?" for k in updates) if updates else ""
        params = list(updates.values())
        sql = "UPDATE cases SET prev_hash=?, hash=?, updated_at=?" + (f", {sets}" if sets else "") + " WHERE id=?"
        self.conn.execute(sql, [case["hash"], new_hash, now, *params, case["id"]])
        self._audit(case["id"], action, actor, audit_payload, prev_case_hash=case["hash"], case_hash=new_hash)
        self.conn.commit()

    def _audit(self, case_id: str, action: str, actor: Actor, payload: dict, *,
               prev_case_hash: str | None = None, case_hash: str | None = None) -> None:
        seq_row = query_one(self.conn, "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM case_audit WHERE case_id=?",
                            (case_id,))
        prev = query_one(self.conn, "SELECT hash FROM case_audit WHERE case_id=? ORDER BY seq DESC LIMIT 1",
                         (case_id,))
        body = {
            "case_id": case_id, "seq": seq_row["n"], "action": action,
            "actor": actor.as_json(), "at": self.clock.now(),
            "payload": {**payload, "case_hash_before": prev_case_hash, "case_hash_after": case_hash},
        }
        insert(self.conn, "case_audit", {
            "id": uuid.uuid4().hex, "case_id": case_id, "seq": seq_row["n"],
            "action": action, "actor": actor.as_json(), "at": body["at"],
            "payload": canonical(body["payload"]),
            "prev_hash": prev["hash"] if prev else None, "hash": digest(body),
        })

    def _open_case(self, case_id: str) -> sqlite3.Row:
        row = query_one(self.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
        if row is None:
            raise NotFound("案件不存在")
        if row["status"] == "closed":
            raise Conflict("案件已关闭")
        return row

    def _require_unit(self, actor: Actor, case: sqlite3.Row) -> None:
        if actor.unit_id != case["unit_id"]:
            raise Forbidden("只能处理本院系的案件")

    def _check_deadline(self, deadline: str | None, label: str) -> None:
        if deadline is None:
            return
        if parse_ts(self.clock.now()) > parse_ts(deadline):
            raise Conflict(f"{label}已过（截止 {deadline}），请由校级督导按逾期程序处理")

    def _cycle(self, cycle_id: str) -> sqlite3.Row:
        row = query_one(self.conn, "SELECT * FROM cycles WHERE id=?", (cycle_id,))
        if row is None:
            raise NotFound("周期不存在")
        return row

    def _unit(self, unit_id: str) -> sqlite3.Row:
        row = query_one(self.conn, "SELECT id, name FROM units WHERE id=?", (unit_id,))
        if row is None:
            raise NotFound("院系不存在")
        return row

    def _rule(self, rule_id: str) -> sqlite3.Row:
        row = query_one(self.conn, "SELECT * FROM rules WHERE id=?", (rule_id,))
        if row is None:
            raise NotFound("规则不存在")
        return row


# ---------- 行序列化 ----------

def _snapshot_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "cycle_id": row["cycle_id"], "unit_id": row["unit_id"],
        "kind": row["kind"], "payload": json.loads(row["payload"]),
        "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
        "sealed_at": row["sealed_at"], "hash": row["hash"],
    }


def _rule_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "version": row["version"], "name": row["name"],
        "description": row["description"], "definition": json.loads(row["definition"]),
        "status": row["status"], "effective_from_cycle": row["effective_from_cycle"],
        "approved_by": row["approved_by"], "approved_at": row["approved_at"],
    }


def _case_hash_body(row: sqlite3.Row | dict) -> dict:
    keys = (
        "unit_id", "cycle_id", "rule_id", "rule_version", "status",
        "finding_snapshot", "finding_hash", "first_run_id", "latest_run_id",
        "explain_deadline", "explain_submitted_at", "explain_text",
        "exempt_deadline", "exempt_status",
        "rectify_deadline", "rectify_submitted_at", "rectify_plan", "closed_reason",
    )
    available = row.keys()  # sqlite3.Row 与 dict 均提供
    return {k: (row[k] if k in available else None) for k in keys}


def _case_refresh(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()


def _case_dict(row: sqlite3.Row, now: str) -> dict:
    deadlines = {
        "explain": row["explain_deadline"], "exempt": row["exempt_deadline"],
        "rectify": row["rectify_deadline"],
    }
    overdue = {
        name: (bool(deadline) and row["status"] != "closed" and parse_ts(now) > parse_ts(deadline))
        for name, deadline in deadlines.items()
    }
    return {
        "id": row["id"], "unit_id": row["unit_id"], "cycle_id": row["cycle_id"],
        "rule_id": row["rule_id"], "rule_version": row["rule_version"], "status": row["status"],
        "finding": json.loads(row["finding_snapshot"]),
        "first_run_id": row["first_run_id"], "latest_run_id": row["latest_run_id"],
        "deadlines": {**deadlines, "overdue": overdue},
        "explain_text": row["explain_text"], "exempt_status": row["exempt_status"],
        "rectify_plan": row["rectify_plan"], "closed_reason": row["closed_reason"],
        "hash": row["hash"], "prev_hash": row["prev_hash"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _audit_dict(row: sqlite3.Row) -> dict:
    return {
        "seq": row["seq"], "action": row["action"], "actor": row["actor"],
        "at": row["at"], "payload": json.loads(row["payload"]),
        "prev_hash": row["prev_hash"], "hash": row["hash"],
    }
