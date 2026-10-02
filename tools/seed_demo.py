"""端到端演示：三个周期的使命漂移场景。

运行：python3 tools/seed_demo.py [数据库路径]

场景：物理学院以基础学科为立院使命，但近三年基础学科招生占比
持续下降（42% → 36% → 27%），热门专业投入占比攀升。系统在
规则生效的周期给出预警，院系解释、督导核实、限期整改、验收关闭，
全过程留痕；并演示规则升级不溯及既往与旧周期重放结果不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift import Actor, App
from mission_drift.clock import Clock
from mission_drift.hashing import canonical

T0 = "2026-01-10T00:00:00+00:00"

# 规则：基础学科招生占比低于 30% 且连续三年下降（规则在 2024 年批准生效，不溯及 2023）
RULE = {
    "id": "rule-basic-enroll-decline",
    "name": "基础学科招生占比持续走低",
    "input": {
        "share": {"path": "enrollment.basic_discipline_ratio"},
    },
    "condition": {"<": [{"ref": "share"}, 0.30]},
    "trend": {"metric": "share", "direction": "decreasing", "periods": 3},
    "severity": "high",
    "message": "基础学科招生占比 {share:.1%} 低于30%，且连续三年下降",
}

# 2026 年升级的更严规则：低于 35% 即预警，只对 2026 及以后生效
RULE_V2 = {
    "id": "rule-basic-enroll-35",
    "name": "基础学科招生占比低于35%",
    "input": {"share": {"path": "enrollment.basic_discipline_ratio"}},
    "condition": {"<": [{"ref": "share"}, 0.35]},
    "severity": "medium",
    "message": "基础学科招生占比 {share:.1%} 低于35%",
}

DATA = {
    "2023": {
        "mission": {"charter": "发展物理等基础学科，服务国家基础研究", "keywords": ["基础研究", "物理学"]},
        "commitment": {"basic_enroll_target": 0.45, "basic_input_target": 0.5},
        "discipline_input": {"basic_ratio": 0.48, "hot_major_ratio": 0.12, "faculty_hire_basic": 18},
        "enrollment": {"basic_discipline_ratio": 0.42, "hot_major_ratio": 0.18, "grad_adjust_ratio": 0.05},
        "service_output": {"basic_research_projects": 31, "horizontal_projects": 6, "social_training_hours": 400},
    },
    "2024": {
        "mission": {"charter": "发展物理等基础学科，服务国家基础研究", "keywords": ["基础研究", "物理学"]},
        "commitment": {"basic_enroll_target": 0.45, "basic_input_target": 0.5},
        "discipline_input": {"basic_ratio": 0.41, "hot_major_ratio": 0.22, "faculty_hire_basic": 9},
        "enrollment": {"basic_discipline_ratio": 0.36, "hot_major_ratio": 0.29, "grad_adjust_ratio": 0.11},
        "service_output": {"basic_research_projects": 28, "horizontal_projects": 14, "social_training_hours": 900},
    },
    "2025": {
        "mission": {"charter": "发展物理等基础学科，服务国家基础研究", "keywords": ["基础研究", "物理学"]},
        "commitment": {"basic_enroll_target": 0.45, "basic_input_target": 0.5},
        "discipline_input": {"basic_ratio": 0.33, "hot_major_ratio": 0.38, "faculty_hire_basic": 4},
        "enrollment": {"basic_discipline_ratio": 0.27, "hot_major_ratio": 0.41, "grad_adjust_ratio": 0.19},
        "service_output": {"basic_research_projects": 22, "horizontal_projects": 27, "social_training_hours": 1600},
    },
}


def main(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    app = App(db_path=db_path, clock=Clock(T0))
    planner = Actor("发展规划处", "张明")
    supervisor = Actor("校级督导", "王督导")
    dean = Actor("院系负责人", "李院长", unit_id="phys")

    app.create_unit(planner, "phys", "物理学院")
    for cid, label in [("2023", "2023学年"), ("2024", "2024学年"),
                       ("2025", "2025学年"), ("2026", "2026学年")]:
        app.create_cycle(planner, cid, label)

    # 2023 年尚无规则：规则 2024 年才起草批准，且生效周期=2024
    app.create_rule_draft(planner, RULE["name"], RULE)
    app.approve_rule(planner, RULE["id"], "2024")

    official_hashes: dict[str, str] = {}
    for cid in ("2023", "2024", "2025"):
        app.clock.set(f"2026-01-10T00:00:00+00:00")
        for kind, payload in DATA[cid].items():
            app.submit_snapshot(planner, cid, "phys", kind, payload)
        seal = app.seal_cycle(planner, cid)
        report = app.run_cycle(planner, cid)
        official_hashes[cid] = report["result_hash"]
        print(f"[周期 {cid}] 封存 {seal['snapshot_count']} 份快照，"
              f"命中 {report['finding_count']} 条，result_hash={report['result_hash'][:12]}")

    # 2026 年升级规则（35% 阈值），并把旧规则退役，均只对 2026 起生效
    app.create_rule_draft(planner, RULE_V2["name"], RULE_V2)
    app.approve_rule(planner, RULE_V2["id"], "2026")
    app.retire_rule(planner, RULE["id"], "2026")

    # 重放所有旧周期：结果必须与正式运行逐位相同
    print("\n--- 旧周期重放 ---")
    for cid in ("2023", "2024", "2025"):
        replay = app.run_cycle(planner, cid, replay=True)
        assert replay["identical_to_official"], cid
        print(f"[重放 {cid}] 与正式运行一致：{replay['identical_to_official']}")

    # 案件办理（预警发生在 2025 周期：27% < 30% 且三年连降）
    cases = app.list_cases()
    assert len(cases) == 1, f"预期 1 个案件，实际 {len(cases)}"
    case = cases[0]
    cid = case["id"]
    print(f"\n[立案] {case['rule_id']} 严重度={case['finding']['severity']} "
          f"状态={case['status']}（待核，非违规定性）")

    app.clock.set("2026-01-15T00:00:00+00:00")
    app.submit_explanation(dean, cid, "近年考研热门方向调剂生源增加，占比下降系短期波动")
    print("[解释] 院系已在期限内提交解释，案件进入核实")

    app.clock.set("2026-01-20T00:00:00+00:00")
    verified = app.verify_case(supervisor, cid, True, "三年连降且投入同向偏移，解释不足以排除使命漂移")
    print(f"[核实] 督导确认偏离，整改截止 {verified['rectify_deadline']}")

    # 院系先尝试申请豁免（超期或被拒路径之一），这里走整改
    app.clock.set("2026-02-05T00:00:00+00:00")
    app.submit_rectification(dean, cid, "2026 年起基础学科招生指标恢复至 40%，新增基础教研岗 12 个")
    app.review_rectification(supervisor, cid, False, "指标口径需细化到专业", extend_days=30)
    print("[整改] 首次材料被退回，期限顺延 30 天")

    app.clock.set("2026-02-20T00:00:00+00:00")
    app.submit_rectification(dean, cid, "物理学/应用物理学各招 90 人，合计占比 40%；季度通报执行情况")
    app.review_rectification(supervisor, cid, True, "口径清晰、可核查，验收通过")
    print("[关闭] 整改验收通过，案件关闭")

    chain = app.verify_chain(cid)
    print(f"\n[审计] 哈希链完整，共 {chain['audit_count']} 条轨迹")
    timeline = app.case_timeline(cid)
    for item in timeline["timeline"]:
        print(f"  {item['seq']}. {item['at'][:10]} {item['action']} — {item['actor']}")

    print("\n[确定性证据] 各周期正式运行 result_hash：")
    for cid, h in official_hashes.items():
        print(f"  {cid}: {h}")
    print(f"\n演示完成，数据库：{db_path}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/demo.db"
    main(target)
