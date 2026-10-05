"""服务启动入口：``python -m safety_incidents``。"""
from __future__ import annotations

from .api import build_server
from .seed import seed_users


def main() -> None:
    host = "127.0.0.1"
    port = 8080
    httpd, state = build_server(host, port)
    seed_users(state.repo)
    print(f"场馆安全事件协同服务已启动：http://{host}:{port}")
    print("演示账号（请求头 X-User-Id）：u-venue-op / u-volunteer / u-guardian / u-manager")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
