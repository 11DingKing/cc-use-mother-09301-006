"""HTTP 服务：零第三方依赖，基于标准库 http.server。

认证采用简单的请求头（适用于内网/演示，生产环境应替换为统一身份认证）：

* ``X-Actor-Role``：发展规划处 / 院系负责人 / 校级督导
* ``X-Actor-Name``：操作人姓名（写入审计轨迹）
* ``X-Actor-Unit``：院系负责人所属单位

所有响应为 JSON；业务错误返回结构化错误体与合适的 4xx 状态码。
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App
from .clock import Clock
from .errors import DomainError
from .storage import query_all

SNAPSHOT_KINDS = ("mission", "commitment", "discipline_input", "enrollment", "service_output")


class Handler(BaseHTTPRequestHandler):
    app: App  # 由 make_server 注入到类上

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    # ---------- 框架 ----------

    def _send(self, status: int, body: dict | list) -> None:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _actor(self):
        from .app import Actor
        role_code = self.headers.get("X-Actor-Role", "")
        name = self.headers.get("X-Actor-Name", "")
        if not role_code or not name:
            raise DomainError("缺少身份头 X-Actor-Role / X-Actor-Name", code="unauthorized", http_status=401)
        role = {
            "planner": "发展规划处",
            "unit": "院系负责人",
            "supervisor": "校级督导",
        }.get(role_code, role_code)  # 同时允许直接传中文角色名
        return Actor(role=role, name=name, unit_id=self.headers.get("X-Actor-Unit"))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}", code="validation_error")
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象", code="validation_error")
        return value

    def _handle(self, runner):
        # 串行化：SQLite 连接跨线程共享，写事务必须排队（内网审计场景足够）
        with self.server.lock:  # type: ignore[attr-defined]
            try:
                status, body = runner()
                self._send(status, body)
            except DomainError as exc:
                self._send(exc.http_status, exc.to_dict())
            except KeyError as exc:
                self._send(400, {"error": "validation_error", "message": f"缺少必填字段：{exc.args[0]}"})
            except Exception as exc:  # 兜底：任何意外都返回结构化 500，而不是断连
                import traceback
                traceback.print_exc()
                self._send(500, {"error": "internal_error", "message": str(exc)})

    # ---------- 路由 ----------

    def do_GET(self) -> None:
        self._handle(lambda: self._route_get())

    def do_POST(self) -> None:
        self._handle(lambda: self._route_post())

    def _route_get(self):
        app, path = self.app, self.path
        if path == "/health":
            return 200, {"status": "ok"}
        if path == "/cycles":
            return 200, {"cycles": app.list_cycles()}
        if path == "/rules":
            return 200, {"rules": app.list_rules()}
        if path == "/cases":
            return 200, {"cases": app.list_cases()}
        m = re.fullmatch(r"/cycles/([^/]+)/snapshots/([^/]+)/([^/]+)", path)
        if m:
            kind = m.group(3)
            if kind not in SNAPSHOT_KINDS:
                raise DomainError(f"快照类型非法：{kind}", code="not_found", http_status=404)
            return 200, app.get_snapshot(m.group(1), m.group(2), kind)
        m = re.fullmatch(r"/cases/([^/]+)", path)
        if m:
            return 200, app.get_case(m.group(1))
        m = re.fullmatch(r"/cases/([^/]+)/timeline", path)
        if m:
            return 200, app.case_timeline(m.group(1))
        m = re.fullmatch(r"/cases/([^/]+)/verify-chain", path)
        if m:
            return 200, app.verify_chain(m.group(1))
        m = re.fullmatch(r"/runs/([^/]+)", path)
        if m:
            return 200, app.get_run(m.group(1))
        if path == "/runs":
            rows = query_all(app.conn,
                             "SELECT id, cycle_id, kind, triggered_by, engine_version, "
                             "ruleset_hash, exemptions_hash, result_hash, parent_run_id, "
                             "started_at, finished_at FROM runs ORDER BY started_at")
            return 200, {"runs": [dict(r) for r in rows]}
        raise DomainError("接口不存在", code="not_found", http_status=404)

    def _route_post(self):
        app, path, body, actor = self.app, self.path, self._body(), self._actor()

        if path == "/admin/units":
            return 201, app.create_unit(actor, body["unit_id"], body["name"])
        if path == "/admin/cycles":
            return 201, app.create_cycle(actor, body["cycle_id"], body["label"])

        m = re.fullmatch(r"/cycles/([^/]+)/snapshots/([^/]+)/([^/]+)", path)
        if m:
            kind = m.group(3)
            if kind not in SNAPSHOT_KINDS:
                raise DomainError(f"快照类型非法：{kind}", code="not_found", http_status=404)
            return 200, app.submit_snapshot(
                actor, m.group(1), m.group(2), kind, body["payload"])

        m = re.fullmatch(r"/cycles/([^/]+)/seal", path)
        if m:
            return 200, app.seal_cycle(actor, m.group(1))
        m = re.fullmatch(r"/cycles/([^/]+)/run", path)
        if m:
            return 200, app.run_cycle(actor, m.group(1))
        m = re.fullmatch(r"/cycles/([^/]+)/replay", path)
        if m:
            return 200, app.run_cycle(actor, m.group(1), replay=True)

        if path == "/rules":
            return 201, app.create_rule_draft(
                actor, body["name"], body["definition"], body.get("description", ""))
        m = re.fullmatch(r"/rules/([^/]+)/approve", path)
        if m:
            return 200, app.approve_rule(actor, m.group(1), body["effective_from_cycle"])
        m = re.fullmatch(r"/rules/([^/]+)/retire", path)
        if m:
            return 200, app.retire_rule(actor, m.group(1), body["retired_from_cycle"])

        if path == "/exemptions":
            return 201, app.grant_exemption(
                actor, body["unit_id"], body["cycle_from"], body["cycle_to"],
                body["reason"], body.get("rule_id"))
        m = re.fullmatch(r"/exemptions/([^/]+)/revoke", path)
        if m:
            return 200, app.revoke_exemption(actor, m.group(1), body["revoke_from_cycle"])

        m = re.fullmatch(r"/cases/([^/]+)/explain", path)
        if m:
            return 200, app.submit_explanation(actor, m.group(1), body["text"])
        m = re.fullmatch(r"/cases/([^/]+)/exemption-request", path)
        if m:
            return 200, app.request_exemption(actor, m.group(1), body["reason"])
        m = re.fullmatch(r"/cases/([^/]+)/exemption-decision", path)
        if m:
            return 200, app.decide_exemption(actor, m.group(1), bool(body["granted"]), body.get("opinion", ""))
        m = re.fullmatch(r"/cases/([^/]+)/verify", path)
        if m:
            return 200, app.verify_case(actor, m.group(1), bool(body["confirmed"]), body.get("opinion", ""))
        m = re.fullmatch(r"/cases/([^/]+)/extend-deadline", path)
        if m:
            return 200, app.extend_deadline(
                actor, m.group(1), body["kind"], int(body["days"]), body.get("reason", ""))
        m = re.fullmatch(r"/cases/([^/]+)/rectification", path)
        if m:
            return 200, app.submit_rectification(actor, m.group(1), body["plan"])
        m = re.fullmatch(r"/cases/([^/]+)/rectification-review", path)
        if m:
            return 200, app.review_rectification(
                actor, m.group(1), bool(body["accepted"]), body.get("opinion", ""),
                body.get("extend_days"))

        raise DomainError("接口不存在", code="not_found", http_status=404)


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    app = App(db_path=db_path, clock=Clock())
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.app = app  # type: ignore[attr-defined]
    httpd.lock = threading.RLock()  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    host = os.environ.get("MD_HOST", "127.0.0.1")
    port = int(os.environ.get("MD_PORT", "8080"))
    db_path = os.environ.get("MD_DB_PATH", "data/mission_drift.db")
    server = make_server(host, port, db_path)
    print(f"高校使命漂移预警服务已启动：http://{host}:{port}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
