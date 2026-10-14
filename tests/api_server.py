"""Настоящий HTTP-сервер на localhost для проверки контрольных вызовов API: ответы можно менять между «днями»."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ApiServer:
    def __init__(self):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
                status, headers, payload = outer.routes.get(path, (404, {}, b'{"error_message": "not found"}'))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _serve

            def log_message(self, *args):  # тишина в выводе тестов
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def reply(self, path: str, status: int = 200, body: object = None, headers: dict[str, str] | None = None) -> None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
        else:
            payload = (body or "").encode("utf-8") if isinstance(body, str) or body is None else body
            headers = headers or {}
        self.routes[path] = (status, headers, payload)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port_url() -> str:
    """Адрес, по которому никто не слушает (соединение будет отвергнуто)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"
