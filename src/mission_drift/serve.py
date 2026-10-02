"""启动 HTTP 服务：python -m mission_drift.serve --db data.sqlite3 --port 8080"""
from __future__ import annotations

import argparse

from .api import make_server
from .clock import SystemClock
from .services import Services
from .storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="高校使命漂移预警服务端")
    parser.add_argument("--db", default="mission_drift.sqlite3", help="SQLite 路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    store = Store(args.db)
    services = Services(store, SystemClock())
    httpd = make_server(args.host, args.port, services)
    print(f"服务已启动：http://{args.host}:{args.port}  数据库：{args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
