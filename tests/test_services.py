"""端到端业务测试：以 FixedClock 保证确定性。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift.clock import FixedClock
from mission_drift.engine import evaluate, validate_payload
from mission_drift.errors import ConflictError, DomainError, PermissionError_
from mission_drift.services import (
    ROLE_DEPARTMENT,
    ROLE_PLANNING,
    ROLE_SUPERVISOR,
    STATE_CLOSED,
    STATE_MONITOR,
    STATE_RECTIFY,
    STATE_VERIFY,
    STATE_WARN,
    Services,
)
from mission_drift.storage import Store


def make_service(moment: str = "2025-01-10T09:00:00+00:00"):
    store = Store(":memory:")
    clock = FixedClock(moment)
    svc = Services(store, clock)
    with svc.conn:
        svc.conn.execute(
            "INSERT INTO actors(id,token,name,role,created_at) "
            "VALUES(1,'tok-plan','规划员甲',?,?)",
            (ROLE_PLANNING, clock.now().isoformat()))
        svc.conn.execute(
            "INSERT INTO actors(id,token,name,role,created_at) "
            "VALUES(2,'tok-sup','督导乙',?,?)",
            (ROLE_SUPERVISOR, clock.now().isoformat()))
        svc.conn.execute(
            "INSERT INTO actors(id,token,name,role,created_at) "
            "VALUES(3,'tok-other-sup','督导丙',?,?)",
            (ROLE_SUPERVISOR, clock.now().isoformat()))
    plan = dict(svc.conn.execute("SELECT * FROM actors WHERE id=1").fetchone())
    sup = dict(svc.conn.execute("SELECT * FROM actors WHERE id=2").fetchone())
    sup2 = dict(svc.conn.execute("SELECT * FROM actors WHERE id=3").fetchone())
    return svc, clock, plan, sup, sup2


def rules_v1():
    return [
        {
            "id": "R-CORE-FUND",
            "name": "核心学科经费占比不得低于 60%",
            "kind": "core_share",
            "level": "warn",
            "params": {"source": "investment", "field": "funding",
                       "min_share": 0.6},
        },
        {
            "id": "R-WATCH-INTAKE",
            "name": "核心学科招生占比观察线 50%",
            "kind": "core_share",
            "level": "watch",
            "params": {"source": "enrollment", "field": "intake",
                       "min_share": 0.5},
        },
        {
            "id": "R-COMMIT",
            "name": "承诺目标完成率不得低于 90%",
            "kind": "commitment",
            "level": "warn",
            "params": {"min_ratio": 0.9},
        },
    ]


SETTINGS = {"explanation_days": 10, "rectification_days": 30,
            "exemption_max_days": 365}


def mission(core=("CS", "MATH")):
    return {"core_discipline_codes": list(core),
            "text": "以基础学科与信息学科为核心"}


def investment(core_fund=300, other_fund=700):
    return {"disciplines": [
        {"code": "CS", "name": "计算机", "funding": core_fund,
         "faculty": 40, "slots": 10},
        {"code": "BUS", "name": "商科", "funding": other_fund,
         "faculty": 60, "slots": 30},
    ]}


def enrollment(cs_intake=400, bus_intake=600):
    return {"programs": [
        {"code": "CS", "name": "计算机", "intake": cs_intake},
        {"code": "BUS", "name": "商科", "intake": bus_intake},
    ]}


def commitments(target=1000):
    return {"targets": [
        {"id": "T1", "name": "计算机经费承诺", "metric": "funding",
         "code": "CS", "target": target},
    ]}


SERVICE = {"projects": [
    {"id": "P1", "name": "算力开源平台", "code": "CS",
     "core_related": True, "scale": 80},
]}


def fill_snapshot(svc, plan, dept_actor, cycle, dept, **overrides):
    data = {
        "mission": mission(),
        "commitments": commitments(),
        "investment": investment(),
        "enrollment": enrollment(),
        "service": SERVICE,
    }
    data.update(overrides)
    for kind, payload in data.items():
        svc.put_item(dept_actor, cycle, dept, kind, payload)
    return data


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, self.plan, self.sup, self.sup2 = make_service()
        self.svc.create_department(self.plan, "D01", "信息学院")
        self.dept = self.svc.create_actor(
            self.plan, "院长丁", ROLE_DEPARTMENT, "D01", "tok-dept")
        self.svc.create_cycle(self.plan, "2025", "2025 年度",
                              "2025-01-01", "2025-12-31")
        self.svc.create_rule_set(self.plan, rules_v1(), SETTINGS, "首版规则")
        self.svc.approve_rule_set(self.sup, 1, "同意")

    def _fill_and_seal(self, **kw):
        fill_snapshot(self.svc, self.plan, self.dept, "2025", "D01", **kw)
        return self.svc.seal_snapshot(self.plan, "2025", "D01")

    def test_01_engine_is_pure_and_sorted(self):
        items = {
            "mission": mission(), "commitments": commitments(),
            "investment": investment(), "enrollment": enrollment(),
            "service": SERVICE,
        }
        f1 = evaluate(items, rules_v1())
        f2 = evaluate(items, list(reversed(rules_v1())))
        self.assertEqual(f1, f2)  # 规则顺序不影响结果
        self.assertGreaterEqual(len(f1), 2)
        self.assertTrue(all(f["level"] in ("watch", "warn") for f in f1))

    def test_02_payload_validation(self):
        with self.assertRaises(DomainError):
            validate_payload("mission", {"core_discipline_codes": []})
        with self.assertRaises(DomainError):
            validate_payload("investment",
                             {"disciplines": [{"code": "CS", "funding": -1}]})

    def test_03_cross_department_forbidden(self):
        self.svc.create_department(self.plan, "D02", "商学院")
        other = self.svc.create_actor(
            self.plan, "院长戊", ROLE_DEPARTMENT, "D02", "tok-dept2")
        fill_snapshot(self.svc, self.plan, self.dept, "2025", "D01")
        with self.assertRaises(PermissionError_):
            self.svc.put_item(other, "2025", "D01", "mission", mission())
        with self.assertRaises(PermissionError_):
            self.svc.get_snapshot(other, "2025", "D01")

    def test_04_seal_creates_pending_cases_not_verdicts(self):
        snap = self._fill_and_seal()
        self.assertEqual(snap["status"], "sealed")
        self.assertTrue(snap["chain_hash"])
        self.assertEqual(snap["rule_set_version"], 1)
        cases = self.svc.list_cases(self.plan, cycle_code="2025")
        statuses = {c["status"] for c in cases}
        # 只允许出现 监测/预警，绝不出现"违规"定性
        self.assertTrue(statuses <= {STATE_MONITOR, STATE_WARN})
        warn_cases = [c for c in cases if c["status"] == STATE_WARN]
        self.assertTrue(any(c["rule_id"] == "R-CORE-FUND" for c in warn_cases))
        # 解释期限 = 封存日 + 10 天
        case = self.svc.get_case(self.plan, warn_cases[0]["case_no"])
        self.assertEqual(case["explanation_due"], "2025-01-20")

    def test_05_sealed_snapshot_is_immutable(self):
        self._fill_and_seal()
        with self.assertRaises(ConflictError):
            self.svc.put_item(self.dept, "2025", "D01", "mission",
                              mission(core=("BUS",)))

    def test_06_full_case_workflow_with_deadlines(self):
        self._fill_and_seal()
        case_no = self.svc.list_cases(
            self.plan, cycle_code="2025", status=STATE_WARN)[0]["case_no"]

        # 逾期提交解释：系统如实记录 overdue，但不拒收
        self.clock.advance(days=12)
        view = self.svc.get_case(self.dept, case_no)
        self.assertTrue(view["explanation_overdue"])
        self.svc.submit_explanation(self.dept, case_no, "当年集中采购跨年度结算")
        overdue = [c for c in self.svc.list_cases(
            self.plan, overdue_only=True)]
        # 已提交解释后不再计入逾期
        self.assertFalse(any(c["case_no"] == case_no for c in overdue))

        # 督导立案核实
        self.svc.start_verification(self.sup, case_no, "调阅经费台账")
        self.assertEqual(self.svc.get_case(self.plan, case_no)["status"],
                         STATE_VERIFY)

        # 院系角色不能裁定
        with self.assertRaises(DomainError):
            self.svc.decide_case(self.dept, case_no, "rectify", None, None)

        # 裁定整改，期限 30 天
        self.svc.decide_case(self.sup, case_no, "rectify",
                             "解释不充分", None)
        view = self.svc.get_case(self.plan, case_no)
        self.assertEqual(view["status"], STATE_RECTIFY)
        self.assertEqual(view["rectification_due"], "2025-02-21")

        # 未提交报告不能验收通过
        with self.assertRaises(ConflictError):
            self.svc.accept_rectification(self.sup, case_no, True, None)

        # 逾期整改也可提交，overdue 留痕
        self.clock.advance(days=31)
        self.assertTrue(self.svc.get_case(self.plan, case_no)
                        ["rectification_overdue"])
        self.svc.submit_rectification(self.dept, case_no, "已调整预算结构")
        self.svc.accept_rectification(self.sup, case_no, True, "复核通过")
        view = self.svc.get_case(self.plan, case_no)
        self.assertEqual(view["status"], STATE_CLOSED)
        self.assertEqual(view["close_reason"], "整改通过")

        timeline = self.svc.case_timeline(self.plan, case_no)
        kinds = [e["event_type"] for e in timeline]
        self.assertEqual(kinds[0], "case.open")
        self.assertIn("explanation.submit", kinds)
        self.assertIn("rectification.submit", kinds)
        # 时间线只增不改
        self.assertEqual([e["seq"] for e in timeline],
                         list(range(1, len(timeline) + 1)))

    def test_07_unsubstantiated_and_exemption(self):
        self._fill_and_seal()
        cases = self.svc.list_cases(self.plan, cycle_code="2025")
        warn = [c for c in cases if c["status"] == STATE_WARN]
        watch = [c for c in cases if c["status"] == STATE_MONITOR]

        self.svc.decide_case(self.sup, watch[0]["case_no"],
                             "unsubstantiated", "数据口径误差", None)
        self.assertEqual(self.svc.get_case(self.plan, watch[0]["case_no"])
                         ["close_reason"], "不成立")

        # 豁免有上限
        with self.assertRaises(DomainError):
            self.svc.decide_case(self.sup, warn[0]["case_no"], "exempt",
                                 "政策过渡", "2026-12-31")
        self.svc.decide_case(self.sup, warn[0]["case_no"], "exempt",
                             "政策过渡期", "2025-12-31")
        self.assertEqual(self.svc.get_case(self.plan, warn[0]["case_no"])
                         ["close_reason"], "豁免")

    def test_08_rule_upgrade_only_applies_forward_and_replay_matches(self):
        self._fill_and_seal()
        seal1 = self.svc.get_snapshot(self.plan, "2025", "D01")

        # 新周期 + 更严的规则 v2
        self.clock.advance(days=365)
        self.svc.create_cycle(self.plan, "2026", "2026 年度",
                              "2026-01-01", "2026-12-31")
        v2 = [dict(r) for r in rules_v1()]
        v2[0] = {**v2[0], "params": {"source": "investment", "field": "funding",
                                     "min_share": 0.9}}
        self.svc.create_rule_set(self.plan, v2, SETTINGS, "提高核心占比要求")
        # 职责分离：规划角色无权批准
        with self.assertRaises(DomainError):
            self.svc.approve_rule_set(self.plan, 2, None)
        self.svc.approve_rule_set(self.sup2, 2, "同意")

        fill_snapshot(self.svc, self.plan, self.dept, "2026", "D01")
        self.svc.seal_snapshot(self.plan, "2026", "D01")

        # 旧周期重放：仍用 v1，逐字节一致
        r1 = self.svc.replay_snapshot("2025", "D01")
        self.assertTrue(r1["findings_hash_match"])
        self.assertTrue(r1["chain_hash_match"])
        self.assertTrue(r1["cases_match"])
        self.assertEqual(r1["rule_set_version"], 1)

        r2 = self.svc.replay_snapshot("2026", "D01")
        self.assertEqual(r2["rule_set_version"], 2)
        self.assertTrue(r2["findings_hash_match"])
        # v2 下 observed=0.3 依然命中（0.3<0.9），阈值改变已被记录
        finding = next(f for f in r2["findings"] if f["rule_id"] == "R-CORE-FUND")
        self.assertEqual(finding["threshold"], 0.9)

        # 整周期重放
        batch = self.svc.replay_cycle("2025")
        self.assertTrue(batch["all_match"])
        self.assertEqual(batch["snapshot_count"], 1)

        # 封存元数据未变
        seal1_again = self.svc.get_snapshot(self.plan, "2025", "D01")
        self.assertEqual(seal1["chain_hash"], seal1_again["chain_hash"])
        self.assertEqual(seal1["rule_set_version"], 1)

    def test_09_exemption_carries_forward(self):
        self._fill_and_seal()
        warn = self.svc.list_cases(self.plan, cycle_code="2025",
                                   status=STATE_WARN)
        core_case = next(c for c in warn if c["rule_id"] == "R-CORE-FUND")
        self.svc.decide_case(self.sup, core_case["case_no"], "exempt",
                             "过渡期", "2026-01-10")

        self.clock.advance(days=365)
        self.svc.create_cycle(self.plan, "2026", "2026 年度", None, None)
        fill_snapshot(self.svc, self.plan, self.dept, "2026", "D01")
        self.svc.seal_snapshot(self.plan, "2026", "D01")
        new_cases = self.svc.list_cases(self.plan, cycle_code="2026")
        carried = next(c for c in new_cases if c["rule_id"] == "R-CORE-FUND")
        self.assertEqual(carried["status"], STATE_CLOSED)
        self.assertIn("豁免沿用", carried["close_reason"])
        self.assertEqual(carried["exempt_until"], "2026-01-10")

    def test_10_closed_cycle_blocks_changes_and_seal(self):
        fill_snapshot(self.svc, self.plan, self.dept, "2025", "D01")
        self.svc.close_cycle(self.plan, "2025")
        with self.assertRaises(ConflictError):
            self.svc.put_item(self.dept, "2025", "D01", "service", SERVICE)
        with self.assertRaises(ConflictError):
            self.svc.seal_snapshot(self.plan, "2025", "D01")

    def test_11_incomplete_material_cannot_seal(self):
        self.svc.put_item(self.dept, "2025", "D01", "mission", mission())
        with self.assertRaises(ConflictError):
            self.svc.seal_snapshot(self.plan, "2025", "D01")

    def test_12_provisional_evaluation_creates_nothing(self):
        fill_snapshot(self.svc, self.plan, self.dept, "2025", "D01")
        result = self.svc.provisional_evaluation(self.plan, "2025", "D01")
        self.assertTrue(result["provisional"])
        self.assertEqual(self.svc.list_cases(self.plan), [])

    def test_13_audit_chain_intact_and_detects_tampering(self):
        self._fill_and_seal()
        self.assertTrue(self.svc.audit.verify()["intact"])
        # 直接篡改库中封存材料 -> 重放立即暴露
        row = self.svc.conn.execute(
            "SELECT snapshot_id FROM snapshot_items WHERE kind='mission'").fetchone()
        self.svc.conn.execute(
            "UPDATE snapshot_items SET payload_json=? WHERE snapshot_id=? AND kind='mission'",
            ('{"core_discipline_codes":["BUS"],"text":"被篡改"}',
             row["snapshot_id"]))
        self.svc.conn.commit()
        report = self.svc.replay_snapshot("2025", "D01")
        self.assertFalse(report["items_hash_match"])

    def test_14_audit_chain_break_detected(self):
        self._fill_and_seal()
        self.svc.conn.execute("UPDATE audit_log SET action='hacked' WHERE seq=1")
        self.svc.conn.commit()
        with self.assertRaises(ConflictError):
            self.svc.audit.verify()

    def test_15_replay_is_identity_independent_of_clock(self):
        self._fill_and_seal()
        h1 = self.svc.replay_snapshot("2025", "D01")
        self.clock.advance(days=9999)
        h2 = self.svc.replay_snapshot("2025", "D01")
        self.assertEqual(h1["replayed_findings_hash"],
                         h2["replayed_findings_hash"])
        self.assertEqual(h1["findings"], h2["findings"])


class EngineEdgeTest(unittest.TestCase):
    def _items(self, invest, enroll, core=("CS",)):
        return {
            "mission": mission(core),
            "commitments": commitments(),
            "investment": invest,
            "enrollment": enroll,
            "service": SERVICE,
        }

    def test_all_core_no_false_positive(self):
        invest = {"disciplines": [{"code": "CS", "funding": 1000}]}
        enroll = {"programs": [{"code": "CS", "intake": 1000}]}
        findings = evaluate(self._items(invest, enroll), rules_v1())
        # 承诺完成 1000/1000、占比 100%，不应有任何命中
        self.assertEqual(findings, [])

    def test_noncore_and_missing_core_rules(self):
        rules = [
            {"id": "NC", "name": "非核心经费占比上限 40%", "kind": "noncore_share",
             "level": "warn",
             "params": {"source": "investment", "field": "funding",
                        "max_share": 0.4}},
            {"id": "MISS", "name": "核心学科投入招生双缺", "kind": "missing_core",
             "level": "warn",
             "params": {"sources": ["investment", "enrollment"]}},
        ]
        # CS 为核心但投入/招生均为零，BUS 占 100%
        invest = {"disciplines": [
            {"code": "CS", "funding": 0}, {"code": "BUS", "funding": 500}]}
        enroll = {"programs": [{"code": "BUS", "intake": 100}]}
        findings = evaluate(self._items(invest, enroll, core=("CS",)), rules)
        ids = {f["rule_id"] for f in findings}
        self.assertEqual(ids, {"NC", "MISS"})
        nc = next(f for f in findings if f["rule_id"] == "NC")
        self.assertEqual(nc["observed"], 1.0)
        miss = next(f for f in findings if f["rule_id"] == "MISS")
        self.assertEqual(miss["subject"], "CS")
        self.assertEqual(set(miss["absent_in"]), {"investment", "enrollment"})

    def test_invalid_rule_rejected(self):
        from mission_drift.engine import validate_rule
        from mission_drift.errors import DomainError
        with self.assertRaises(DomainError):
            validate_rule({"id": "X", "name": "x", "kind": "core_share",
                           "params": {"source": "investment", "field": "intake",
                                      "min_share": 0.5}})
        with self.assertRaises(DomainError):
            validate_rule({"id": "X", "name": "x", "kind": "commitment",
                           "params": {"min_ratio": 1.5}})

    def test_evaluate_requires_complete_material(self):
        with self.assertRaises(DomainError):
            evaluate({"mission": mission()}, rules_v1())


class PersistenceTest(unittest.TestCase):
    """关闭数据库后用新进程/新连接重开：封存、审计链、重放必须仍然成立。"""

    def test_reopen_database_replays_identically(self):
        import tempfile
        from pathlib import Path as P

        tmp = P(tempfile.mkdtemp()) / "persist.sqlite3"

        store = Store(tmp)
        clock = FixedClock("2025-05-01T00:00:00+00:00")
        svc = Services(store, clock)
        with svc.conn:
            svc.conn.execute(
                "INSERT INTO actors(token,name,role,created_at) "
                "VALUES('P','规划','planning',?)", (clock.now().isoformat(),))
            svc.conn.execute(
                "INSERT INTO actors(token,name,role,created_at) "
                "VALUES('S','督导','supervisor',?)", (clock.now().isoformat(),))
        plan = dict(svc.conn.execute(
            "SELECT * FROM actors WHERE token='P'").fetchone())
        sup = dict(svc.conn.execute(
            "SELECT * FROM actors WHERE token='S'").fetchone())
        svc.create_department(plan, "D01", "信息学院")
        dept = svc.create_actor(plan, "院长", ROLE_DEPARTMENT, "D01", "tok-d")
        svc.create_cycle(plan, "2025", "2025 年度", None, None)
        svc.create_rule_set(plan, rules_v1(), SETTINGS, "v1")
        svc.approve_rule_set(sup, 1, None)
        fill_snapshot(svc, plan, dept, "2025", "D01")
        svc.seal_snapshot(plan, "2025", "D01")
        before = svc.replay_snapshot("2025", "D01")
        audit_before = svc.audit.verify()
        store.close()

        # 模拟另一人在另一个时间点打开同一份库重放
        store2 = Store(tmp)
        clock2 = FixedClock("2030-01-01T00:00:00+00:00")
        svc2 = Services(store2, clock2)
        after = svc2.replay_snapshot("2025", "D01")
        self.assertTrue(after["findings_hash_match"])
        self.assertTrue(after["items_hash_match"])
        self.assertTrue(after["chain_hash_match"])
        self.assertEqual(before["replayed_findings_hash"],
                         after["replayed_findings_hash"])
        self.assertEqual(svc2.audit.verify()["entries"], audit_before["entries"])
        store2.close()


if __name__ == "__main__":
    unittest.main()
