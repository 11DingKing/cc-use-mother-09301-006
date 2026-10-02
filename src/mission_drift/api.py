"""HTTP API（标准库 http.server，零第三方依赖）。

鉴权：所有 /api/* 请求携带 X-Auth-Token；GET /healthz 除外。
响应统一为 {"ok": true, "data": ...} / {"ok": false, "error": ...}。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .services import ROLE_PLANNING, ROLE_SUPERVISOR, Services

# 这些路径是"动作"，创建成功也返回 200 而非 201
ACTION_SEGMENTS = (
    "/close", "/approve", "/seal", "/replay", "/evaluate",
    "/verify", "/decide", "/acceptance",
)


def h_department_list(svc, actor, body, query):
    return svc.list_departments()


def h_department_create(svc, actor, body, query):
    return svc.create_department(actor, body.get("code", ""), body.get("name", ""))


def h_actor_create(svc, actor, body, query):
    return svc.create_actor(actor, body.get("name", ""), body.get("role", ""),
                            body.get("department_code"), body.get("token"))


def h_actor_list(svc, actor, body, query):
    svc.require_role(actor, ROLE_PLANNING)
    rows = svc.conn.execute(
        "SELECT a.id,a.name,a.role,a.disabled,a.created_at,d.code AS department_code "
        "FROM actors a LEFT JOIN departments d ON d.id=a.department_id "
        "ORDER BY a.id").fetchall()
    return [dict(r) for r in rows]


def h_actor_disable(svc, actor, body, query, actor_id):
    return svc.disable_actor(actor, int(actor_id))


def h_cycle_list(svc, actor, body, query):
    return svc.list_cycles()


def h_cycle_create(svc, actor, body, query):
    return svc.create_cycle(actor, body.get("code", ""), body.get("name", ""),
                            body.get("starts_on"), body.get("ends_on"))


def h_cycle_close(svc, actor, body, query, cycle):
    return svc.close_cycle(actor, cycle)


def h_ruleset_list(svc, actor, body, query):
    return svc.list_rule_sets()


def h_ruleset_create(svc, actor, body, query):
    return svc.create_rule_set(actor, body.get("rules", []),
                               body.get("settings", {}), body.get("rationale"))


def h_ruleset_get(svc, actor, body, query, version):
    return svc.get_rule_set(int(version))


def h_ruleset_update(svc, actor, body, query, version):
    return svc.update_rule_set_draft(actor, int(version), body.get("rules", []),
                                     body.get("settings", {}),
                                     body.get("rationale"))


def h_ruleset_approve(svc, actor, body, query, version):
    return svc.approve_rule_set(actor, int(version), body.get("note"))


def h_snapshot_list(svc, actor, body, query):
    return svc.list_snapshots(actor, query.get("cycle"))


def h_snapshot_get(svc, actor, body, query, cycle, dept):
    return svc.get_snapshot(actor, cycle, dept)


def h_snapshot_put_item(svc, actor, body, query, cycle, dept, kind):
    return svc.put_item(actor, cycle, dept, kind, body)


def h_snapshot_evaluate(svc, actor, body, query, cycle, dept):
    return svc.provisional_evaluation(actor, cycle, dept)


def h_snapshot_seal(svc, actor, body, query, cycle, dept):
    return svc.seal_snapshot(actor, cycle, dept)


def h_snapshot_replay(svc, actor, body, query, cycle, dept):
    return svc.replay_snapshot(cycle, dept)


def h_cycle_replay(svc, actor, body, query, cycle):
    return svc.replay_cycle(cycle)


def h_case_list(svc, actor, body, query):
    overdue = {"false": False, "true": True}.get(
        query.get("overdue_only", "false"), False)
    return svc.list_cases(actor, query.get("cycle"), query.get("department"),
                          query.get("status"), overdue)


def h_case_get(svc, actor, body, query, case_no):
    return svc.get_case(actor, case_no)


def h_case_timeline(svc, actor, body, query, case_no):
    return svc.case_timeline(actor, case_no)


def h_case_explanation(svc, actor, body, query, case_no):
    return svc.submit_explanation(actor, case_no, body.get("text", ""))


def h_case_verify(svc, actor, body, query, case_no):
    return svc.start_verification(actor, case_no, body.get("note"))


def h_case_decide(svc, actor, body, query, case_no):
    return svc.decide_case(actor, case_no, body.get("verdict", ""),
                           body.get("note"), body.get("exempt_until"))


def h_case_rectification(svc, actor, body, query, case_no):
    return svc.submit_rectification(actor, case_no, body.get("text", ""))


def h_case_accept(svc, actor, body, query, case_no):
    return svc.accept_rectification(actor, case_no,
                                    bool(body.get("accepted", False)),
                                    body.get("note"))


def h_audit_list(svc, actor, body, query):
    svc.require_role(actor, ROLE_PLANNING, ROLE_SUPERVISOR)
    limit = min(int(query.get("limit", "200")), 1000)
    rows = svc.conn.execute(
        "SELECT seq,at,actor_id,action,target,detail_json,prev_hash,entry_hash "
        "FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def h_audit_verify(svc, actor, body, query):
    svc.require_role(actor, ROLE_PLANNING, ROLE_SUPERVISOR)
    return svc.audit.verify()


# (path 正则, {HTTP 方法: 处理函数})；处理函数签名 (svc, actor, body, query, **kwargs)
ROUTES = [
    (r"/api/departments", {"GET": h_department_list, "POST": h_department_create}),
    (r"/api/actors", {"GET": h_actor_list, "POST": h_actor_create}),
    (r"/api/actors/(?P<actor_id>\d+)/disable", {"POST": h_actor_disable}),
    (r"/api/cycles", {"GET": h_cycle_list, "POST": h_cycle_create}),
    (r"/api/cycles/(?P<cycle>[^/]+)/close", {"POST": h_cycle_close}),
    (r"/api/cycles/(?P<cycle>[^/]+)/replay", {"POST": h_cycle_replay}),
    (r"/api/rule-sets", {"GET": h_ruleset_list, "POST": h_ruleset_create}),
    (r"/api/rule-sets/(?P<version>\d+)",
     {"GET": h_ruleset_get, "POST": h_ruleset_update}),
    (r"/api/rule-sets/(?P<version>\d+)/approve", {"POST": h_ruleset_approve}),
    (r"/api/snapshots", {"GET": h_snapshot_list}),
    (r"/api/snapshots/(?P<cycle>[^/]+)/(?P<dept>[^/]+)", {"GET": h_snapshot_get}),
    (r"/api/snapshots/(?P<cycle>[^/]+)/(?P<dept>[^/]+)/items/(?P<kind>[^/]+)",
     {"PUT": h_snapshot_put_item}),
    (r"/api/snapshots/(?P<cycle>[^/]+)/(?P<dept>[^/]+)/evaluate",
     {"POST": h_snapshot_evaluate}),
    (r"/api/snapshots/(?P<cycle>[^/]+)/(?P<dept>[^/]+)/seal",
     {"POST": h_snapshot_seal}),
    (r"/api/snapshots/(?P<cycle>[^/]+)/(?P<dept>[^/]+)/replay",
     {"POST": h_snapshot_replay}),
    (r"/api/cases", {"GET": h_case_list}),
    (r"/api/cases/(?P<case_no>[^/]+)", {"GET": h_case_get}),
    (r"/api/cases/(?P<case_no>[^/]+)/timeline", {"GET": h_case_timeline}),
    (r"/api/cases/(?P<case_no>[^/]+)/explanation", {"POST": h_case_explanation}),
    (r"/api/cases/(?P<case_no>[^/]+)/verify", {"POST": h_case_verify}),
    (r"/api/cases/(?P<case_no>[^/]+)/decide", {"POST": h_case_decide}),
    (r"/api/cases/(?P<case_no>[^/]+)/rectification", {"POST": h_case_rectification}),
    (r"/api/cases/(?P<case_no>[^/]+)/acceptance", {"POST": h_case_accept}),
    (r"/api/audit", {"GET": h_audit_list}),
    (r"/api/audit/verify", {"POST": h_audit_verify}),
]


def make_handler(services: Services) -> type[BaseHTTPRequestHandler]:
    """生成绑定了业务层实例的 Handler 类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MissionDrift/1.0"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DomainError("请求体不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return value

        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if path == "/healthz":
                    self._send(200, {"ok": True, "data": {"status": "ok"}})
                    return
                # 整段处理持锁：SQLite 单连接 + 审计链写入需要全局串行。
                with services.store.lock:
                    actor = services.authenticate(
                        self.headers.get("X-Auth-Token"))
                    body = self._body() if method in ("POST", "PUT") else {}
                    for pattern, methods in ROUTES:
                        m = re.fullmatch(pattern, path)
                        if m and method in methods:
                            data = methods[method](
                                services, actor, body, query, **m.groupdict())
                            status = 201 if (
                                method == "POST"
                                and not any(seg in path for seg in ACTION_SEGMENTS)
                            ) else 200
                            self._send(status, {"ok": True, "data": data})
                            return
                self._send(404, {"ok": False,
                                 "error": f"未找到接口：{method} {path}"})
            except DomainError as exc:
                self._send(exc.status, {"ok": False, "error": exc.message})
            except Exception as exc:  # noqa: BLE001 - 服务端兜底
                self._send(500, {"ok": False, "error": f"内部错误：{exc}"})

    return Handler


def make_server(host: str, port: int, services: Services) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(services))
