"""HTTP 接口端到端测试（标准库 urllib + 随机端口）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_drift.http_server import make_server

PLANNER = {"X-Actor-Role": "planner", "X-Actor-Name": "Zhang"}
SUP = {"X-Actor-Role": "supervisor", "X-Actor-Name": "Wang"}
DEAN = {"X-Actor-Role": "unit", "X-Actor-Name": "Li", "X-Actor-Unit": "u1"}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        self.server = make_server("127.0.0.1", 0, self.tmp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        Path(self.tmp.name).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(self.tmp.name + suffix).unlink(missing_ok=True)

    def req(self, method: str, path: str, headers: dict | None = None, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            request.add_header(k, v)
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _bootstrap(self, ratio: float = 0.25, rule_effective: str = "c1") -> None:
        self.req("POST", "/admin/units", PLANNER, {"unit_id": "u1", "name": "物理学院"})
        self.req("POST", "/admin/cycles", PLANNER, {"cycle_id": "c1", "label": "第一周期"})
        rule = {
            "id": "r-ratio", "name": "占比偏低",
            "input": {"x": {"path": "enrollment.ratio"}},
            "condition": {"<": [{"ref": "x"}, 0.3]}, "severity": "high",
        }
        self.req("POST", "/rules", PLANNER, {"name": "占比偏低", "definition": rule})
        self.req("POST", "/rules/r-ratio/approve", PLANNER, {"effective_from_cycle": rule_effective})
        payloads = {
            "mission": {"m": "基础"}, "commitment": {"t": 0.4},
            "discipline_input": {"r": 0.4}, "enrollment": {"ratio": ratio},
            "service_output": {"p": 10},
        }
        for kind, payload in payloads.items():
            status, _ = self.req("POST", f"/cycles/c1/snapshots/u1/{kind}", PLANNER, {"payload": payload})
            self.assertEqual(status, 200)

    def test_health(self) -> None:
        status, body = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_case_flow_over_http(self) -> None:
        self._bootstrap()
        status, body = self.req("POST", "/cycles/c1/seal", PLANNER)
        self.assertEqual(status, 200)
        status, run = self.req("POST", "/cycles/c1/run", PLANNER)
        self.assertEqual(status, 200)
        self.assertEqual(run["finding_count"], 1)
        case_id = run["case_actions"]["opened"][0]

        # 无身份头 → 401
        status, body = self.req("POST", f"/cases/{case_id}/explain", body={"text": "x"})
        self.assertEqual(status, 401)

        status, body = self.req("POST", f"/cases/{case_id}/explain", DEAN, {"text": "短期波动"})
        self.assertEqual((status, body["status"]), (200, "verifying"))

        status, body = self.req("POST", f"/cases/{case_id}/verify", SUP,
                                {"confirmed": True, "opinion": "需整改"})
        self.assertEqual((status, body["status"]), (200, "rectifying"))

        status, body = self.req("POST", f"/cases/{case_id}/rectification", DEAN,
                                {"plan": "恢复占比"})
        self.assertEqual(status, 200)
        status, body = self.req("POST", f"/cases/{case_id}/rectification-review", SUP,
                                {"accepted": True, "opinion": "通过"})
        self.assertEqual((status, body["status"]), (200, "closed"))

        status, body = self.req("GET", f"/cases/{case_id}/verify-chain")
        self.assertEqual((status, body["ok"]), (200, True))

    def test_replay_endpoint(self) -> None:
        self._bootstrap()
        self.req("POST", "/cycles/c1/seal", PLANNER)
        _, official = self.req("POST", "/cycles/c1/run", PLANNER)
        status, replay = self.req("POST", "/cycles/c1/replay", SUP)
        self.assertEqual(status, 200)
        self.assertTrue(replay["identical_to_official"])
        self.assertEqual(replay["result_hash"], official["result_hash"])
        self.assertEqual(replay["kind"], "replay")

    def test_seal_rejects_incomplete_cycle(self) -> None:
        self.req("POST", "/admin/units", PLANNER, {"unit_id": "u1", "name": "学院"})
        self.req("POST", "/admin/cycles", PLANNER, {"cycle_id": "c1", "label": "一"})
        status, body = self.req("POST", "/cycles/c1/seal", PLANNER)
        self.assertEqual(status, 409)
        self.assertIn("五要素", body["message"])

    def test_permission_denied(self) -> None:
        status, body = self.req("POST", "/admin/units", DEAN, {"unit_id": "u1", "name": "学院"})
        self.assertEqual(status, 403)

    def test_unknown_route_404(self) -> None:
        status, _ = self.req("GET", "/nope")
        self.assertEqual(status, 404)

    def test_sealed_snapshot_cannot_change(self) -> None:
        self._bootstrap()
        self.req("POST", "/cycles/c1/seal", PLANNER)
        status, body = self.req("POST", "/cycles/c1/snapshots/u1/mission", PLANNER,
                                {"payload": {"tampered": True}})
        self.assertEqual(status, 409)

    def test_persistence_across_server_restart(self) -> None:
        self._bootstrap()
        self.req("POST", "/cycles/c1/seal", PLANNER)
        _, run = self.req("POST", "/cycles/c1/run", PLANNER)
        first_hash = run["result_hash"]
        self.server.shutdown()
        self.thread.join(timeout=3)
        # 用同一数据库文件重启服务，重放结果不变
        self.server = make_server("127.0.0.1", 0, self.tmp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, replay = self.req("POST", "/cycles/c1/replay", PLANNER)
        self.assertTrue(replay["identical_to_official"])
        self.assertEqual(replay["result_hash"], first_hash)


if __name__ == "__main__":
    unittest.main()
