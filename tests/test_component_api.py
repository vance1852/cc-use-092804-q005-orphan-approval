from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from component_qualification.api import Handler
from component_qualification.service import ComponentService


class HttpApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "component.sqlite3")
        service = ComponentService(self.db_path)
        service.bootstrap_admin()
        service.auth.create_user("q1", "quality-pass", "quality")
        service.auth.create_user("op1", "operator-pass", "operator")
        admin = service.auth.login("admin", "component-admin")
        service.create_lot(admin, "LOT-1", "MCU", "R1", 100)

        Handler.service = ComponentService(self.db_path, cross_thread=True)
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _request(self, method: str, path: str, token: str | None, body: dict | None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _login(self, user_id: str, password: str) -> str:
        _, body = self._request("POST", "/login", None, {"user_id": user_id, "password": password})
        return body["token"]

    def test_error_codes_are_distinguished(self) -> None:
        quality = self._login("q1", "quality-pass")
        operator = self._login("op1", "operator-pass")

        status, body = self._request("POST", "/lots/NOPE/approvals", quality,
                                     {"decision": "release", "reason": "批次不存在"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        status, body = self._request("POST", "/lots/LOT-1/approvals", operator,
                                     {"decision": "release", "reason": "越权"})
        self.assertEqual(status, 403)

        status, body = self._request("POST", "/lots/LOT-1/approvals", quality,
                                     {"decision": "release"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_failed")

        status, body = self._request("POST", "/lots/LOT-1/approvals", quality,
                                     {"decision": "release", "reason": "放行",
                                      "expected_revision": 99})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")

    def test_replay_and_conflict_and_audit_over_http(self) -> None:
        quality = self._login("q1", "quality-pass")
        first_status, first = self._request("POST", "/lots/LOT-1/approvals", quality,
                                            {"decision": "hold", "reason": "挂起",
                                             "expected_revision": 1})
        self.assertEqual(first_status, 200)
        self.assertFalse(first["replayed"])

        second_status, second = self._request("POST", "/lots/LOT-1/approvals", quality,
                                              {"decision": "hold", "reason": "重发"})
        self.assertEqual(second_status, 200)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["reason"], "挂起")

        conflict_status, conflict = self._request("POST", "/lots/LOT-1/approvals", quality,
                                                  {"decision": "release", "reason": "改判"})
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "conflict")

        audit_status, audit = self._request("GET", "/lots/LOT-1/audit", quality, None)
        self.assertEqual(audit_status, 200)
        self.assertEqual([e["event_type"] for e in audit["events"]], ["created", "approval"])

        ghost_status, ghost_audit = self._request("GET", "/lots/GHOST/audit", quality, None)
        self.assertEqual(ghost_status, 200)
        self.assertEqual(ghost_audit["events"], [])


if __name__ == "__main__":
    unittest.main()
