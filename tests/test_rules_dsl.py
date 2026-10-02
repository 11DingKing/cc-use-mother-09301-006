"""规则 DSL 的单元测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift.rules_dsl import evaluate, validate_definition
from mission_drift.errors import ValidationError


def history(current: dict, *priors: dict) -> list[dict]:
    return list(priors) + [current]


class DslValidationTest(unittest.TestCase):
    def test_accepts_well_formed_rule(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.3]},
        }
        validate_definition(rule)  # 不抛异常即可

    def test_rejects_unknown_operator(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"__import__": [{"ref": "x"}]},
        }
        with self.assertRaises(ValidationError):
            validate_definition(rule)

    def test_rejects_undeclared_ref(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "y"}, 0.3]},
        }
        with self.assertRaises(ValidationError):
            validate_definition(rule)

    def test_rejects_path_outside_five_kinds(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "arbitrary.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.3]},
        }
        with self.assertRaises(ValidationError):
            validate_definition(rule)

    def test_rejects_bad_trend(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.3]},
            "trend": {"metric": "x", "direction": "sideways", "periods": 3},
        }
        with self.assertRaises(ValidationError):
            validate_definition(rule)


class DslEvaluationTest(unittest.TestCase):
    @staticmethod
    def FIVE(v: float) -> dict:  # 五要素齐全的当前快照
        return {
            "mission": {}, "commitment": {}, "discipline_input": {},
            "enrollment": {"ratio": v}, "service_output": {},
        }

    def test_threshold_hit(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.3]},
            "severity": "high",
        }
        self.assertIsNotNone(evaluate(rule, self.FIVE(0.25), history(self.FIVE(0.25))))
        self.assertIsNone(evaluate(rule, self.FIVE(0.31), history(self.FIVE(0.31))))

    def test_and_or_not_between_in(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}, "y": {"path": "discipline_input.ratio"}},
            "condition": {"and": [
                {"between": [{"ref": "x"}, 0.2, 0.4]},
                {"or": [{">": [{"ref": "y"}, 0.5]}, {"not": {"==": [{"ref": "y"}, 0.0]}}]},
            ]},
        }
        cur = {"mission": {}, "commitment": {}, "discipline_input": {"ratio": 0.6},
               "enrollment": {"ratio": 0.3}, "service_output": {}}
        self.assertIsNotNone(evaluate(rule, cur, history(cur)))

    def test_baseline_pct_change(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {
                "now": {"path": "service_output.projects"},
                "old": {"path": "service_output.projects", "baseline": True},
            },
            "condition": {"<": [{"pct_change": [{"ref": "old"}, {"ref": "now"}]}, -0.2]},
        }
        before = {"mission": {}, "commitment": {}, "discipline_input": {}, "enrollment": {},
                  "service_output": {"projects": 100.0}}
        after = {**before, "service_output": {"projects": 70.0}}
        self.assertIsNotNone(evaluate(rule, after, history(after, before)))
        stable = {**before, "service_output": {"projects": 95.0}}
        self.assertIsNone(evaluate(rule, stable, history(stable, before)))

    def test_missing_baseline_means_no_finding(self) -> None:
        """首个周期没有基线：数据不足，绝不误判。"""
        rule = {
            "id": "r", "name": "n",
            "input": {"old": {"path": "service_output.projects", "baseline": True}},
            "condition": {"==": [{"ref": "old"}, 1]},
        }
        cur = self.FIVE(0.5)
        self.assertIsNone(evaluate(rule, cur, history(cur)))

    def test_trend_decreasing_three_periods(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.35]},
            "trend": {"metric": "x", "direction": "decreasing", "periods": 3},
        }
        h = history(self.FIVE(0.27), self.FIVE(0.42), self.FIVE(0.36))
        self.assertIsNotNone(evaluate(rule, self.FIVE(0.27), h))
        # 持平不算漂移趋势
        flat = history(self.FIVE(0.27), self.FIVE(0.27), self.FIVE(0.27))
        self.assertIsNone(evaluate(rule, self.FIVE(0.27), flat))
        # 序列不足三年
        short = history(self.FIVE(0.27), self.FIVE(0.4))
        self.assertIsNone(evaluate(rule, self.FIVE(0.27), short))

    def test_pct_change_zero_base_rejected(self) -> None:
        rule = {
            "id": "r", "name": "n",
            "input": {
                "now": {"path": "service_output.projects"},
                "old": {"path": "service_output.projects", "baseline": True},
            },
            "condition": {"<": [{"pct_change": [{"ref": "old"}, {"ref": "now"}]}, -0.2]},
        }
        before = {"mission": {}, "commitment": {}, "discipline_input": {}, "enrollment": {},
                  "service_output": {"projects": 0.0}}
        with self.assertRaises(ValidationError):
            evaluate(rule, before, history(before, before))


if __name__ == "__main__":
    unittest.main()
