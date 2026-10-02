"""HTTP 端到端测试：真实起服，经 JSON over HTTP 走完整流程。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from http.server import ThreadingHTTPServer

from mission_drift.api import make_handler
from mission_drift.clock import FixedClock
from mission_drift.services import Services
from mission_drift.storage import Store


class Client:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, token: str | None = None,
             body: dict | None = None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Auth-Token", token)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.clock = FixedClock("2025-03-01T08:00:00+00:00")
        cls.svc = Services(cls.store, cls.clock)
        with cls.svc.conn:
            cls.svc.conn.execute(
                "INSERT INTO actors(id,token,name,role,created_at) "
                "VALUES(1,'P','规划', 'planning',?)",
                (cls.clock.now().isoformat(),))
            cls.svc.conn.execute(
                "INSERT INTO actors(id,token,name,role,created_at) "
                "VALUES(2,'S','督导', 'supervisor',?)",
                (cls.clock.now().isoformat(),))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = Client(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.store.close()

    def test_end_to_end(self):
        api = self.api
        status, body = api.call("GET", "/healthz")
        self.assertEqual((status, body["data"]), (200, {"status": "ok"}))

        # 无令牌
        status, body = api.call("GET", "/api/cycles")
        self.assertEqual(status, 403)

        # 组织、账号、周期、规则
        _, body = api.call("POST", "/api/departments", "P",
                           {"code": "D01", "name": "信息学院"})
        self.assertTrue(body["ok"])
        _, body = api.call("POST", "/api/actors", "P",
                           {"name": "院长", "role": "department",
                            "department_code": "D01", "token": "D"})
        _, body = api.call("POST", "/api/cycles", "P",
                           {"code": "2025", "name": "2025 年度"})
        rules = [{"id": "R1", "name": "核心经费占比≥50%", "kind": "core_share",
                  "level": "warn",
                  "params": {"source": "investment", "field": "funding",
                             "min_share": 0.5}}]
        settings = {"explanation_days": 7, "rectification_days": 21,
                    "exemption_max_days": 180}
        _, body = api.call("POST", "/api/rule-sets", "P",
                           {"rules": rules, "settings": settings})
        self.assertEqual(body["data"]["version"], 1)
        self.assertEqual(body["data"]["status"], "draft")
        # 规划员无权批准（职责分离：批准权属校级督导）
        status, body = api.call("POST", "/api/rule-sets/1/approve", "P", {})
        self.assertEqual(status, 403)
        _, body = api.call("POST", "/api/rule-sets/1/approve", "S",
                           {"note": "同意"})
        self.assertEqual(body["data"]["status"], "approved")

        # 院系提交五类材料
        items = {
            "mission": {"core_discipline_codes": ["CS"], "text": "信息学科为主"},
            "commitments": {"targets": [
                {"id": "T1", "name": "经费", "metric": "funding",
                 "code": "CS", "target": 1000}]},
            "investment": {"disciplines": [
                {"code": "CS", "funding": 100},
                {"code": "BUS", "funding": 900}]},
            "enrollment": {"programs": [
                {"code": "CS", "intake": 100}]},
            "service": {"projects": [
                {"id": "P1", "code": "CS", "core_related": True, "scale": 10}]},
        }
        for kind, payload in items.items():
            status, body = api.call(
                "PUT", f"/api/snapshots/2025/D01/items/{kind}", "D", payload)
            self.assertEqual(status, 200, body)

        # 试算不落案
        _, body = api.call("POST", "/api/snapshots/2025/D01/evaluate", "P")
        self.assertTrue(body["data"]["provisional"])
        self.assertEqual(len(body["data"]["findings"]), 1)
        _, body = api.call("GET", "/api/cases", "P")
        self.assertEqual(body["data"], [])

        # 封存
        _, body = api.call("POST", "/api/snapshots/2025/D01/seal", "P")
        self.assertEqual(body["data"]["status"], "sealed")
        _, body = api.call("GET", "/api/cases", "P")
        cases = body["data"]
        self.assertEqual(len(cases), 1)
        case_no = cases[0]["case_no"]
        self.assertEqual(cases[0]["status"], "预警")

        # 解释 -> 核实 -> 整改 -> 关闭
        _, body = api.call("POST", f"/api/cases/{case_no}/explanation", "D",
                           {"text": "跨年度结算"})
        self.assertIsNotNone(body["data"]["explanation_submitted_at"])
        _, body = api.call("POST", f"/api/cases/{case_no}/verify", "S",
                           {"note": "立案"})
        self.assertEqual(body["data"]["status"], "核实")
        _, body = api.call("POST", f"/api/cases/{case_no}/decide", "S",
                           {"verdict": "rectify", "note": "需整改"})
        self.assertEqual(body["data"]["status"], "整改")
        due = body["data"]["rectification_due"]
        # 未交报告不能通过
        status, _ = api.call("POST", f"/api/cases/{case_no}/acceptance", "S",
                             {"accepted": True})
        self.assertEqual(status, 409)
        _, body = api.call("POST", f"/api/cases/{case_no}/rectification", "D",
                           {"text": "已整改"})
        _, body = api.call("POST", f"/api/cases/{case_no}/acceptance", "S",
                           {"accepted": True, "note": "通过"})
        self.assertEqual(body["data"]["status"], "关闭")

        # 时间线、重放、审计
        _, body = api.call("GET", f"/api/cases/{case_no}/timeline", "P")
        self.assertGreaterEqual(len(body["data"]), 6)
        _, body = api.call("POST", "/api/snapshots/2025/D01/replay", "P")
        self.assertTrue(body["data"]["findings_hash_match"])
        self.assertTrue(body["data"]["chain_hash_match"])
        _, body = api.call("POST", "/api/cycles/2025/replay", "S")
        self.assertTrue(body["data"]["all_match"])
        _, body = api.call("POST", "/api/audit/verify", "P")
        self.assertTrue(body["data"]["intact"])

        # 院系看不到审计接口
        status, _ = api.call("GET", "/api/audit", "D")
        self.assertEqual(status, 403)

    def test_bad_json(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/cycles",
            data=b"{not json", method="POST")
        req.add_header("X-Auth-Token", "P")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
