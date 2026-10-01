"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ComponentError, ValidationFailed
from .service import ComponentService


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到部件质量服务，便于无网络单元测试。"""

    def __init__(self, service: ComponentService) -> None:
        self.service = service

    @staticmethod
    def _token(headers: Mapping[str, str]) -> str:
        return headers.get("authorization", "").removeprefix("Bearer ").strip()

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        # 共享的 SQLite 连接不是线程安全的；每次请求完整持有服务锁。
        with self.service.lock:
            return self._dispatch(method, path, parts, normalized_headers, body)

    def _dispatch(self, method: str, path: str, parts: list[str], headers: Mapping[str, str], body: bytes) -> Response:
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "component-qualification"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/login":
                return Response(200, {"token": self.service.auth.login(payload["user_id"], payload["password"])})
            token = self._token(headers)
            if method == "POST" and path == "/lots":
                return Response(201, self.service.create_lot(token, payload["lot_id"], payload["product"], payload["process_rev"], int(payload["unit_count"])))
            if method == "GET" and len(parts) == 2 and parts[0] == "lots":
                return Response(200, self.service.get_lot(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots":
                lot_id = parts[1]
                if method == "POST" and parts[2] == "measurements":
                    return Response(201, self.service.add_measurement(token, lot_id, payload["signal_frequency_hz"], payload["response"], payload.get("noise", 0.0), payload["instrument"]))
                if method == "POST" and parts[2] == "analysis":
                    return Response(200, self.service.analyze(token, lot_id))
                if method == "POST" and parts[2] == "approval":
                    expected = payload.get("expected_revision")
                    return Response(200, self.service.approve(token, lot_id, payload["decision"], payload["reason"], None if expected is None else int(expected)))
                if method == "GET" and parts[2] == "events":
                    return Response(200, {"events": self.service.audit(token, lot_id)})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ComponentError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except PermissionError as exc:
            return Response(403, {"error": {"code": "forbidden", "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ComponentQualification/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = ComponentService(args.database)
    service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(service))).serve_forever()


if __name__ == "__main__":
    main()
