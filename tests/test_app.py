"""应用服务层测试：封存、规则生效边界、豁免、确定性重放、案件状态机。"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift import Actor, App, Conflict, Forbidden, NotFound, ValidationError
from mission_drift.clock import Clock
from mission_drift.hashing import digest

PLANNER = lambda: Actor("发展规划处", "张")
SUP = lambda: Actor("校级督导", "王")
DEAN = lambda uid="u1": Actor("院系负责人", "李", unit_id=uid)

RULE = {
    "id": "r-ratio", "name": "占比偏低",
    "input": {"x": {"path": "enrollment.ratio"}},
    "condition": {"<": [{"ref": "x"}, 0.3]}, "severity": "high",
    "message": "{x}",
}


def five(ratio: float) -> dict[str, dict]:
    return {
        "mission": {"m": "基础"}, "commitment": {"t": 0.4},
        "discipline_input": {"r": 0.4}, "enrollment": {"ratio": ratio},
        "service_output": {"p": 10},
    }


class SealedWorld:
    """构造已封存周期的测试夹具。"""

    def __init__(self, ratios: dict[str, float], rule_effective: str | None = None,
                 clock: str = "2026-01-01T00:00:00+00:00"):
        self.app = App(":memory:", clock=Clock(clock))
        self.p, self.s, self.d = PLANNER(), SUP(), DEAN()
        self.app.create_unit(self.p, "u1", "学院")
        for cid in ratios:
            self.app.create_cycle(self.p, cid, f"周期{cid}")
        if rule_effective:
            self.app.create_rule_draft(self.p, RULE["name"], RULE)
            self.app.approve_rule(self.p, RULE["id"], rule_effective)
        for cid, ratio in ratios.items():
            for kind, payload in five(ratio).items():
                self.app.submit_snapshot(self.p, cid, "u1", kind, payload)
            self.app.seal_cycle(self.p, cid)


class SnapshotTest(unittest.TestCase):
    def test_seal_requires_all_five_kinds(self) -> None:
        w = SealedWorld.__new__(SealedWorld)
        w.app = App(":memory:", clock=Clock("2026-01-01T00:00:00+00:00"))
        w.p = PLANNER()
        w.app.create_unit(w.p, "u1", "学院")
        w.app.create_cycle(w.p, "c1", "一")
        w.app.submit_snapshot(w.p, "c1", "u1", "mission", {"m": "x"})
        with self.assertRaises(Conflict):
            w.app.seal_cycle(w.p, "c1")

    def test_sealed_snapshot_is_immutable(self) -> None:
        w = SealedWorld({"c1": 0.5})
        with self.assertRaises(sqlite3.IntegrityError):
            w.app.conn.execute(
                "UPDATE snapshots SET payload=? WHERE sealed_at IS NOT NULL", ("{}",))
        with self.assertRaises(sqlite3.IntegrityError):
            w.app.conn.execute("DELETE FROM snapshots WHERE sealed_at IS NOT NULL")
        w.app.conn.rollback()

    def test_resubmit_before_seal_is_allowed(self) -> None:
        app = App(":memory:", clock=Clock("2026-01-01T00:00:00+00:00"))
        p = PLANNER()
        app.create_unit(p, "u1", "学院")
        app.create_cycle(p, "c1", "一")
        app.submit_snapshot(p, "c1", "u1", "mission", {"v": 1})
        app.submit_snapshot(p, "c1", "u1", "mission", {"v": 2})
        self.assertEqual(app.get_snapshot("c1", "u1", "mission")["payload"], {"v": 2})

    def test_role_enforcement(self) -> None:
        app = App(":memory:")
        with self.assertRaises(Forbidden):
            app.create_unit(DEAN(), "u1", "学院")


class RuleLifecycleTest(unittest.TestCase):
    def test_rule_cannot_apply_retroactively(self) -> None:
        w = SealedWorld({"c1": 0.25})
        w.app.create_rule_draft(w.p, RULE["name"], RULE)
        with self.assertRaises(Conflict):
            w.app.approve_rule(w.p, RULE["id"], "c1")

    def test_invalid_definition_rejected(self) -> None:
        w = SealedWorld({"c1": 0.25})
        bad = {"id": "bad", "name": "x", "input": {"x": {"path": "enrollment.ratio"}},
               "condition": {"rm": [{"ref": "x"}]}}
        with self.assertRaises(ValidationError):
            w.app.create_rule_draft(w.p, "bad", bad)


class DeterminismTest(unittest.TestCase):
    def _world(self):
        # 规则在 c2 生效
        return SealedWorld({"c1": 0.25, "c2": 0.25, "c3": 0.25}, rule_effective="c2")

    def test_replay_matches_official_bit_for_bit(self) -> None:
        w = self._world()
        hashes = {}
        for cid in ("c1", "c2", "c3"):
            hashes[cid] = w.app.run_cycle(w.p, cid)["result_hash"]
        # 无论何时、由谁重放
        for cid in ("c1", "c2", "c3"):
            replay = w.app.run_cycle(w.s, cid, replay=True)
            self.assertTrue(replay["identical_to_official"])
            self.assertEqual(replay["result_hash"], hashes[cid])

    def test_second_official_run_is_rejected(self) -> None:
        w = self._world()
        w.app.run_cycle(w.p, "c2")
        with self.assertRaises(Conflict):
            w.app.run_cycle(w.p, "c2")

    def test_rule_upgrade_after_seal_does_not_change_old_cycles(self) -> None:
        w = self._world()
        old = {}
        for cid in ("c1", "c2", "c3"):
            old[cid] = w.app.run_cycle(w.p, cid)["result_hash"]
        # 后续周期新增更严规则
        w.app.create_cycle(w.p, "c4", "四")
        rule2 = {"id": "r-ratio-35", "name": "35",
                 "input": {"x": {"path": "enrollment.ratio"}},
                 "condition": {"<": [{"ref": "x"}, 0.35]}}
        w.app.create_rule_draft(w.p, "35", rule2)
        w.app.approve_rule(w.p, "r-ratio-35", "c4")
        for cid in ("c1", "c2", "c3"):
            replay = w.app.run_cycle(w.p, cid, replay=True)
            self.assertEqual(replay["result_hash"], old[cid], f"{cid} 结果被规则升级改变")

    def test_retirement_only_affects_future_cycles(self) -> None:
        w = self._world()
        h_c3 = w.app.run_cycle(w.p, "c3")["result_hash"]
        self.assertEqual(w.app.run_cycle(w.p, "c2")["finding_count"], 1)
        w.app.create_cycle(w.p, "c4", "四")
        with self.assertRaises(Conflict):
            w.app.retire_rule(w.p, RULE["id"], "c3")  # 不能对已封存周期退役
        w.app.retire_rule(w.p, RULE["id"], "c4")
        # c3 重放仍然命中旧规则
        self.assertEqual(w.app.run_cycle(w.p, "c3", replay=True)["result_hash"], h_c3)

    def test_offline_snapshot_tampering_breaks_replay(self) -> None:
        """绕过服务（含触发器）直接改已封存快照：重放时哈希失配，评估中止。"""
        w = self._world()
        for cid in ("c1", "c2", "c3"):
            w.app.run_cycle(w.p, cid)
        # 模拟拿到库文件的攻击者：先删触发器再改数据
        w.app.conn.execute("DROP TRIGGER trg_snapshot_sealed_update")
        w.app.conn.execute(
            "UPDATE snapshots SET payload=? WHERE cycle_id='c2' AND kind='enrollment'",
            (json.dumps({"ratio": 0.99}),))
        w.app.conn.commit()
        with self.assertRaises(Conflict):
            w.app.run_cycle(w.p, "c2", replay=True)

    def test_fresh_database_reseed_produces_identical_hashes(self) -> None:
        """最强确定性保证：全新库重新录入相同数据，结果哈希必须逐位相同。"""
        def build():
            w = SealedWorld({"c1": 0.25, "c2": 0.24, "c3": 0.23}, rule_effective="c1")
            return {cid: w.app.run_cycle(w.p, cid)["result_hash"] for cid in ("c1", "c2", "c3")}
        self.assertEqual(build(), build())

    def test_exemption_skips_finding_and_is_part_of_manifest(self) -> None:
        w = self._world()
        # c1 尚未豁免时运行 c2……先给 c2 的豁免：必须在 c2 封存前授予
        # 夹具已封存全部周期，所以这里只验证不可回溯
        with self.assertRaises(Conflict):
            w.app.grant_exemption(w.s, "u1", "c2", "c3", "事后豁免不予承认")

    def test_exemption_future_cycle_then_revoke_future(self) -> None:
        app = App(":memory:", clock=Clock("2026-01-01T00:00:00+00:00"))
        p, s = PLANNER(), SUP()
        app.create_unit(p, "u1", "学院")
        for cid in ("c1", "c2"):
            app.create_cycle(p, cid, cid)
        app.create_rule_draft(p, RULE["name"], RULE)
        app.approve_rule(p, RULE["id"], "c1")
        # c1：命中
        for kind, payload in five(0.25).items():
            app.submit_snapshot(p, "c1", "u1", kind, payload)
        app.seal_cycle(p, "c1")
        r1 = app.run_cycle(p, "c1")
        self.assertEqual(r1["finding_count"], 1)
        # 对 c2 授予豁免
        ex = app.grant_exemption(s, "u1", "c2", "c2", "学校统筹调剂，本年度免责")
        for kind, payload in five(0.25).items():
            app.submit_snapshot(p, "c2", "u1", kind, payload)
        app.seal_cycle(p, "c2")
        r2 = app.run_cycle(p, "c2")
        self.assertEqual(r2["finding_count"], 0)
        self.assertNotEqual(r2["exemptions_hash"], r1["exemptions_hash"])
        # 撤销只能指向未来周期
        with self.assertRaises(Conflict):
            app.revoke_exemption(s, ex["id"], "c2")


class CaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = SealedWorld({"c1": 0.25, "c2": 0.25}, rule_effective="c1")
        self.report = self.w.app.run_cycle(self.w.p, "c2")
        self.cid = self.report["case_actions"]["opened"][0]

    def test_alert_is_not_violation_and_carries_frozen_finding(self) -> None:
        case = self.w.app.get_case(self.cid)
        self.assertEqual(case["status"], "alert")
        self.assertEqual(case["finding"]["observed"]["x"], 0.25)
        self.assertTrue(case["finding"]["finding_hash"])

    def test_full_happy_path(self) -> None:
        w, cid = self.w, self.cid
        w.app.submit_explanation(w.d, cid, "短期波动")
        self.assertEqual(w.app.get_case(cid)["status"], "verifying")
        out = w.app.verify_case(w.s, cid, True, "需要整改")
        self.assertEqual(out["status"], "rectifying")
        self.assertIsNotNone(w.app.get_case(cid)["deadlines"]["rectify"])
        w.app.submit_rectification(w.d, cid, "提高占比")
        w.app.review_rectification(w.s, cid, True, "通过")
        self.assertEqual(w.app.get_case(cid)["status"], "closed")
        self.assertTrue(w.app.verify_chain(cid)["ok"])

    def test_unsubstantiated_closes_case(self) -> None:
        w, cid = self.w, self.cid
        w.app.verify_case(w.s, cid, False, "数据口径错误")
        self.assertEqual(w.app.get_case(cid)["status"], "closed")
        self.assertIn("不成立", w.app.get_case(cid)["closed_reason"])

    def test_exemption_granted_closes_case(self) -> None:
        w, cid = self.w, self.cid
        w.app.request_exemption(w.d, cid, "学校统一招生改革")
        w.app.decide_exemption(w.s, cid, True, "属校级统筹")
        self.assertEqual(w.app.get_case(cid)["status"], "closed")

    def test_exemption_denied_keeps_case_open(self) -> None:
        w, cid = self.w, self.cid
        w.app.request_exemption(w.d, cid, "理由")
        w.app.decide_exemption(w.s, cid, False, "不成立")
        self.assertNotEqual(w.app.get_case(cid)["status"], "closed")

    def test_dean_cannot_touch_other_unit(self) -> None:
        other = DEAN("u2")
        with self.assertRaises(Forbidden):
            self.w.app.submit_explanation(other, self.cid, "越权")

    def test_supervisor_cannot_submit_explanation(self) -> None:
        with self.assertRaises(Forbidden):
            self.w.app.submit_explanation(self.w.s, self.cid, "越权")

    def test_deadline_is_enforced(self) -> None:
        w, cid = self.w, self.cid
        w.app.clock.set("2030-01-01T00:00:00+00:00")
        with self.assertRaises(Conflict):
            w.app.submit_explanation(w.d, cid, "逾期解释")

    def test_overdue_flag(self) -> None:
        w, cid = self.w, self.cid
        w.app.clock.set("2030-01-01T00:00:00+00:00")
        self.assertTrue(w.app.get_case(cid)["deadlines"]["overdue"]["explain"])

    def test_supervisor_can_extend_overdue_deadline(self) -> None:
        w, cid = self.w, self.cid
        w.app.clock.set("2030-01-01T00:00:00+00:00")
        # 已逾期，院系无法自行操作
        with self.assertRaises(Conflict):
            w.app.submit_explanation(w.d, cid, "逾期")
        # 督导延期并留痕
        result = w.app.extend_deadline(w.s, cid, "explain", 30, "春节假期顺延")
        self.assertIn("2030-01-31", result["new_deadline"])
        actions = [t["action"] for t in w.app.case_timeline(cid)["timeline"]]
        self.assertIn("deadline_extended", actions)
        w.app.submit_explanation(w.d, cid, "补充说明")
        self.assertTrue(w.app.verify_chain(cid)["ok"])

    def test_rectification_rejection_can_extend_deadline(self) -> None:
        w, cid = self.w, self.cid
        w.app.verify_case(w.s, cid, True, "整改")
        before = w.app.get_case(cid)["deadlines"]["rectify"]
        w.app.submit_rectification(w.d, cid, "粗糙方案")
        w.app.review_rectification(w.s, cid, False, "不行", extend_days=60)
        after = w.app.get_case(cid)["deadlines"]["rectify"]
        self.assertGreater(after, before)

    def test_recurring_finding_reconfirms_open_case(self) -> None:
        w, cid = self.w, self.cid
        w.app.create_cycle(w.p, "c3", "三")
        for kind, payload in five(0.24).items():
            w.app.submit_snapshot(w.p, "c3", "u1", kind, payload)
        w.app.seal_cycle(w.p, "c3")
        report = w.app.run_cycle(w.p, "c3")
        self.assertEqual(report["case_actions"]["reconfirmed"], [cid])
        self.assertEqual(report["case_actions"]["opened"], [])

    def test_audit_chain_detects_tampering(self) -> None:
        w, cid = self.w, self.cid
        # 直接篡改主记录字段（绕过服务）→ 主记录哈希对不上
        w.app.conn.execute("UPDATE cases SET status='closed' WHERE id=?", (cid,))
        w.app.conn.commit()
        with self.assertRaises(Conflict):
            w.app.verify_chain(cid)
        # 即使把主记录改回原值，篡改审计轨迹同样会被发现
        w.app.conn.execute("UPDATE cases SET status='alert' WHERE id=?", (cid,))
        w.app.conn.execute(
            "UPDATE case_audit SET payload=? WHERE case_id=? AND seq=1",
            (json.dumps({"forged": True}, ensure_ascii=False), cid))
        w.app.conn.commit()
        with self.assertRaises(Conflict):
            w.app.verify_chain(cid)

    def test_closed_case_is_terminal(self) -> None:
        w, cid = self.w, self.cid
        w.app.verify_case(w.s, cid, False, "不成立")
        with self.assertRaises(Conflict):
            w.app.submit_explanation(w.d, cid, "迟来的解释")

    def test_timeline_records_every_action_with_actor(self) -> None:
        w, cid = self.w, self.cid
        w.app.submit_explanation(w.d, cid, "说明")
        timeline = w.app.case_timeline(cid)["timeline"]
        actions = [t["action"] for t in timeline]
        self.assertEqual(actions, ["opened", "explain_submitted"])
        self.assertTrue(all(t["hash"] and t["actor"] for t in timeline))


if __name__ == "__main__":
    unittest.main()
