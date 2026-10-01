"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ServiceError
from .service import ComponentService


class Handler(BaseHTTPRequestHandler):
    service = ComponentService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            status, code = 403, "forbidden"
        elif isinstance(exc, ServiceError):
            status, code = exc.status, exc.code
        else:
            status, code = 400, "invalid_request"
        self._json(status, {"error": {"code": code, "message": str(exc)}})

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "component-qualification"})
        parts = self.path.strip("/").split("/")
        try:
            with self.service.gate:
                if len(parts) == 2 and parts[0] == "lots":
                    return self._json(200, self.service.get_lot(self._token(), parts[1]))
                if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                    return self._json(200, {"events": self.service.audit(self._token(), parts[1])})
            return self._json(404, {"error": {"code": "route_not_found", "message": "not found"}})
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            parts = self.path.strip("/").split("/")
            with self.service.gate:
                if self.path == "/lots":
                    return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["unit_count"]))
                if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                    return self._json(201, self.service.add_measurement(token, parts[1], body["signal_frequency_hz"], body["response"], body.get("noise", 0.0), body["instrument"]))
                if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                    return self._json(200, self.service.analyze(token, parts[1]))
                if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approvals":
                    return self._json(200, self.service.approve(
                        token, parts[1], body["decision"], body["reason"],
                        body.get("expected_revision"),
                    ))
            return self._json(404, {"error": {"code": "route_not_found", "message": "not found"}})
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._json(422, {"error": {"code": "validation_failed", "message": str(exc)}})
        except Exception as exc:
            return self._error(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = ComponentService(args.database, cross_thread=True)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
