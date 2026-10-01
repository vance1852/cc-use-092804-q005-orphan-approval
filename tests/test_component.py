from __future__ import annotations

import json
import unittest

from component_qualification.api import JsonApplication
from component_qualification.errors import Conflict, InvalidState, NotFound
from component_qualification.service import ComponentService


class ComponentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService()
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "component-admin")
        self.service.auth.create_user("quality1", "quality-pass", "quality")
        self.quality = self.service.auth.login("quality1", "quality-pass")
        self.service.auth.create_user("operator1", "operator-pass", "operator")
        self.operator = self.service.auth.login("operator1", "operator-pass")

    def _create_lot(self, lot_id: str = "LOT-1") -> dict:
        return self.service.create_lot(self.admin, lot_id, "CMOS sensor", "P3.2", 10)

    def _counts(self, lot_id: str) -> tuple[int, int]:
        approvals = self.service.db.execute("SELECT count(*) FROM approvals WHERE lot_id=?", (lot_id,)).fetchone()[0]
        events = self.service.db.execute("SELECT count(*) FROM lot_events WHERE lot_id=?", (lot_id,)).fetchone()[0]
        return approvals, events

    def test_failed_approval_leaves_no_trace_and_recreated_lot_is_clean(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-TYPO", "release", "bulk import")
        self.assertEqual(self._counts("LOT-TYPO"), (0, 0))
        self._create_lot("LOT-TYPO")
        lot = self.service.get_lot(self.admin, "LOT-TYPO")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["revision"], 1)
        events = self.service.audit(self.admin, "LOT-TYPO")
        self.assertEqual([row["event_type"] for row in events], ["created"])

    def test_approve_updates_status_revision_and_audit_atomically(self) -> None:
        self._create_lot()
        lot = self.service.approve(self.quality, "LOT-1", "release", "pass", expected_revision=1)
        self.assertEqual(lot["status"], "released")
        self.assertEqual(lot["revision"], 2)
        events = self.service.audit(self.admin, "LOT-1")
        self.assertEqual([row["event_type"] for row in events], ["created", "approval"])
        self.assertEqual(json.loads(events[-1]["payload"])["decision"], "release")

    def test_same_decision_replay_returns_original_result(self) -> None:
        self._create_lot()
        first = self.service.approve(self.quality, "LOT-1", "hold", "awaiting review", expected_revision=1)
        replay = self.service.approve(self.quality, "LOT-1", "hold", "awaiting review", expected_revision=1)
        self.assertEqual(first, replay)
        approvals = self.service.db.execute("SELECT count(*) FROM approvals").fetchone()[0]
        events = self.service.db.execute("SELECT count(*) FROM lot_events WHERE event_type='approval'").fetchone()[0]
        self.assertEqual((approvals, events), (1, 1))

    def test_different_decision_conflicts_and_preserves_state(self) -> None:
        self._create_lot()
        self.service.approve(self.quality, "LOT-1", "hold", "awaiting review")
        with self.assertRaises(Conflict):
            self.service.approve(self.quality, "LOT-1", "release", "changed my mind")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(lot["status"], "hold")
        self.assertEqual(lot["revision"], 2)
        self.assertEqual(self._counts("LOT-1"), (1, 2))

    def test_other_reviewer_on_decided_lot_is_invalid_state(self) -> None:
        self._create_lot()
        self.service.approve(self.quality, "LOT-1", "release", "ok")
        with self.assertRaises(InvalidState):
            self.service.approve(self.admin, "LOT-1", "hold", "second opinion")
        self.assertEqual(self._counts("LOT-1"), (1, 2))

    def test_stale_revision_conflicts(self) -> None:
        self._create_lot()
        with self.assertRaises(Conflict):
            self.service.approve(self.quality, "LOT-1", "release", "ok", expected_revision=99)
        self.assertEqual(self._counts("LOT-1"), (0, 1))
        self.assertEqual(self.service.get_lot(self.admin, "LOT-1")["status"], "engineering")

    def test_approval_requires_permission_and_leaves_no_trace(self) -> None:
        self._create_lot()
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator, "LOT-1", "release", "not allowed")
        self.assertEqual(self.service.get_lot(self.admin, "LOT-1")["status"], "engineering")
        self.assertEqual(self._counts("LOT-1"), (0, 1))

    def test_audit_unknown_lot_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.audit(self.admin, "LOT-MISSING")

    def test_duplicate_lot_creation_conflicts(self) -> None:
        self._create_lot()
        with self.assertRaises(Conflict):
            self._create_lot()
        self.assertEqual(self._counts("LOT-1"), (0, 1))


class ComponentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService()
        self.service.bootstrap_admin()
        self.service.auth.create_user("quality1", "quality-pass", "quality")
        self.service.auth.create_user("operator1", "operator-pass", "operator")
        self.app = JsonApplication(self.service)

    def _login(self, user_id: str, password: str) -> dict[str, str]:
        response = self.app.handle("POST", "/login", body=json.dumps({"user_id": user_id, "password": password}).encode())
        self.assertEqual(response.status, 200)
        return {"Authorization": f"Bearer {response.body['token']}"}

    def _create_lot(self, headers: dict[str, str], lot_id: str = "LOT-1") -> None:
        payload = {"lot_id": lot_id, "product": "CMOS sensor", "process_rev": "P3.2", "unit_count": 10}
        response = self.app.handle("POST", "/lots", headers, json.dumps(payload).encode())
        self.assertEqual(response.status, 201)

    def _approve(self, headers: dict[str, str], lot_id: str, payload: dict):
        return self.app.handle("POST", f"/lots/{lot_id}/approval", headers, json.dumps(payload).encode())

    def test_approval_error_kinds_are_distinct(self) -> None:
        admin = self._login("admin", "component-admin")
        missing = self._approve(admin, "LOT-404", {"decision": "release", "reason": "bulk import"})
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.body["error"]["code"], "not_found")
        self._create_lot(admin)
        stale = self._approve(admin, "LOT-1", {"decision": "release", "reason": "ok", "expected_revision": 99})
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.body["error"]["code"], "conflict")
        first = self._approve(admin, "LOT-1", {"decision": "hold", "reason": "wait", "expected_revision": 1})
        self.assertEqual(first.status, 200)
        self.assertEqual(first.body["status"], "hold")
        replay = self._approve(admin, "LOT-1", {"decision": "hold", "reason": "wait", "expected_revision": 1})
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body, first.body)
        changed = self._approve(admin, "LOT-1", {"decision": "release", "reason": "different"})
        self.assertEqual(changed.status, 409)
        self.assertEqual(changed.body["error"]["code"], "conflict")
        quality = self._login("quality1", "quality-pass")
        decided = self._approve(quality, "LOT-1", {"decision": "release", "reason": "second opinion"})
        self.assertEqual(decided.status, 409)
        self.assertEqual(decided.body["error"]["code"], "invalid_state")

    def test_approval_requires_approve_permission(self) -> None:
        admin = self._login("admin", "component-admin")
        self._create_lot(admin)
        operator = self._login("operator1", "operator-pass")
        denied = self._approve(operator, "LOT-1", {"decision": "release", "reason": "not allowed"})
        self.assertEqual(denied.status, 403)
        self.assertEqual(denied.body["error"]["code"], "forbidden")
        lot = self.app.handle("GET", "/lots/LOT-1", admin)
        self.assertEqual(lot.body["status"], "engineering")

    def test_failed_approval_then_recreated_lot_shows_no_orphan_history(self) -> None:
        admin = self._login("admin", "component-admin")
        failed = self._approve(admin, "LOT-TYPO", {"decision": "release", "reason": "bulk import"})
        self.assertEqual(failed.status, 404)
        self._create_lot(admin, "LOT-TYPO")
        events = self.app.handle("GET", "/lots/LOT-TYPO/events", admin)
        self.assertEqual(events.status, 200)
        self.assertEqual([row["event_type"] for row in events.body["events"]], ["created"])
        lot = self.app.handle("GET", "/lots/LOT-TYPO", admin)
        self.assertEqual(lot.body["status"], "engineering")

    def test_events_of_unknown_lot_are_not_found(self) -> None:
        admin = self._login("admin", "component-admin")
        response = self.app.handle("GET", "/lots/LOT-MISSING/events", admin)
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
