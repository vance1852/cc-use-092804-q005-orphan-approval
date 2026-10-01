from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from component_qualification.errors import Conflict, InvalidState, NotFound, ValidationFailed
from component_qualification.service import ComponentService


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService(":memory:")
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "component-admin")
        self.service.auth.create_user("q1", "quality-pass", "quality")
        self.quality = self.service.auth.login("q1", "quality-pass")
        self.service.auth.create_user("op1", "operator-pass", "operator")
        self.operator = self.service.auth.login("op1", "operator-pass")
        self.service.create_lot(self.admin, "LOT-1", "MCU", "R1", 100)

    def _counts(self) -> tuple[int, int, int]:
        db = self.service.db
        return (
            db.execute("SELECT count(*) FROM approvals").fetchone()[0],
            db.execute("SELECT count(*) FROM lot_events").fetchone()[0],
            db.execute("SELECT count(*) FROM chip_lots").fetchone()[0],
        )

    def test_approve_missing_lot_leaves_no_residue(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-TYPO", "release", "批编号录入错误")
        approvals = self.service.db.execute(
            "SELECT * FROM approvals WHERE lot_id='LOT-TYPO'"
        ).fetchall()
        events = self.service.db.execute(
            "SELECT * FROM lot_events WHERE lot_id='LOT-TYPO'"
        ).fetchall()
        self.assertEqual(approvals, [])
        self.assertEqual(events, [])

    def test_recreated_same_named_lot_does_not_inherit_failed_trace(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "LOT-X", "release", "先于批次存在的误请求")
        self.service.create_lot(self.admin, "LOT-X", "FPGA", "R2", 8)
        lot = self.service.get_lot(self.admin, "LOT-X")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["revision"], 1)
        events = self.service.audit(self.admin, "LOT-X")
        self.assertEqual([e["event_type"] for e in events], ["created"])
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM approvals WHERE lot_id='LOT-X'").fetchone()[0],
            0,
        )

    def test_permission_failure_leaves_no_residue(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator, "LOT-1", "release", "越权审批")
        self.assertEqual(self._counts(), (0, 1, 1))

    def test_invalid_payload_leaves_no_residue(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.approve(self.quality, "LOT-1", "ship-it", "决定非法")
        with self.assertRaises(ValidationFailed):
            self.service.approve(self.quality, "LOT-1", "release", "   ")
        self.assertEqual(self._counts(), (0, 1, 1))

    def test_stale_revision_conflicts_atomically(self) -> None:
        with self.assertRaises(Conflict):
            self.service.approve(self.quality, "LOT-1", "release", "过期版本", expected_revision=99)
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["revision"], 1)
        self.assertEqual(self._counts(), (0, 1, 1))

    def test_approval_commits_decision_status_and_event_together(self) -> None:
        result = self.service.approve(self.quality, "LOT-1", "hold", "等待复核")
        self.assertFalse(result["replayed"])
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["revision"], 2)
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(lot["status"], "hold")
        self.assertEqual(lot["revision"], 2)
        events = [e["event_type"] for e in self.service.audit(self.admin, "LOT-1")]
        self.assertEqual(events, ["created", "approval"])
        self.assertEqual(self._counts(), (1, 2, 1))

    def test_same_decision_safe_replay_returns_original(self) -> None:
        first = self.service.approve(self.quality, "LOT-1", "hold", "等待复核")
        second = self.service.approve(self.quality, "LOT-1", "hold", "重试时理由文本不同")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            {k: first[k] for k in ("lot_id", "reviewer", "decision", "reason", "revision")},
            {k: second[k] for k in ("lot_id", "reviewer", "decision", "reason", "revision")},
        )
        # 重放不产生新的审批行、状态变更或事件。
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(lot["revision"], 2)
        self.assertEqual(len(self.service.audit(self.admin, "LOT-1")), 2)

    def test_different_decision_conflicts_and_keeps_original(self) -> None:
        self.service.approve(self.quality, "LOT-1", "hold", "先挂起")
        with self.assertRaises(Conflict):
            self.service.approve(self.quality, "LOT-1", "release", "改判放行")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(lot["status"], "hold")
        self.assertEqual(lot["revision"], 2)
        self.assertEqual(
            self.service.db.execute(
                "SELECT decision FROM approvals WHERE lot_id='LOT-1'"
            ).fetchone()["decision"],
            "hold",
        )
        self.assertEqual(len(self.service.audit(self.admin, "LOT-1")), 2)

    def test_audit_only_shows_atomically_effective_decisions(self) -> None:
        with self.assertRaises(NotFound):
            self.service.approve(self.quality, "GHOST", "reject", "不存在批次的拒绝")
        self.service.approve(self.quality, "LOT-1", "release", "合格放行")
        events = self.service.audit(self.admin, "LOT-1")
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])
        payload = json.loads(events[-1]["payload"])
        self.assertEqual(payload["decision"], "release")
        ghost = self.service.db.execute(
            "SELECT count(*) FROM lot_events WHERE lot_id='GHOST'"
        ).fetchone()[0]
        self.assertEqual(ghost, 0)

    def test_non_approvable_status_is_rejected_without_write(self) -> None:
        # 批次状态被直接置为终态但尚无审批行（如外部纠偏）：状态不允许。
        self.service.db.execute(
            "UPDATE chip_lots SET status='released' WHERE lot_id='LOT-1'"
        )
        self.service.db.commit()
        with self.assertRaises(InvalidState):
            self.service.approve(self.quality, "LOT-1", "hold", "状态不允许")
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0
        )

    def test_concurrent_conflicting_decisions_serialize_with_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "concurrency.sqlite3")
            outcomes: list[object] = []

            def worker(decision: str) -> None:
                service = ComponentService(path)
                service.bootstrap_admin()
                service.auth.create_user(decision, "password123", "quality")
                token = service.auth.login(decision, "password123")
                try:
                    outcomes.append(
                        service.approve(token, "LOT-1", decision, "并发决定", expected_revision=1)
                    )
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(exc)

            seed = ComponentService(path)
            seed.bootstrap_admin()
            admin = seed.auth.login("admin", "component-admin")
            seed.create_lot(admin, "LOT-1", "MCU", "R1", 100)

            threads = [
                threading.Thread(target=worker, args=("hold",)),
                threading.Thread(target=worker, args=("release",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(outcomes), 2)
            success = [o for o in outcomes if not isinstance(o, Exception)]
            conflicts = [o for o in outcomes if isinstance(o, Conflict)]
            self.assertEqual(len(success), 1)
            self.assertEqual(len(conflicts), 1)
            winner = seed.get_lot(admin, "LOT-1")
            self.assertEqual(winner["revision"], 2)
            self.assertIn(winner["status"], {"hold", "released"})
            self.assertEqual(
                seed.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 1
            )
            self.assertEqual(
                seed.db.execute(
                    "SELECT count(*) FROM lot_events WHERE event_type='approval'"
                ).fetchone()[0],
                1,
            )


class LegacyMigrationTests(unittest.TestCase):
    def test_orphan_residue_from_legacy_schema_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")
            db = sqlite3.connect(path)
            db.executescript(
                """
CREATE TABLE chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT, process_rev TEXT, unit_count INTEGER,
 status TEXT, owner TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT, signal_frequency_hz REAL,
 response REAL, noise REAL, instrument TEXT, operator TEXT, measured_at TEXT,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT, event_type TEXT,
 actor TEXT, payload TEXT, created_at TEXT);
CREATE TABLE approvals(
 lot_id TEXT, reviewer TEXT, decision TEXT, reason TEXT, created_at TEXT,
 PRIMARY KEY(lot_id,reviewer));
INSERT INTO chip_lots VALUES('LOT-KEEP','MCU','R1',10,'engineering','admin','t0','t0');
INSERT INTO lot_events VALUES(1,'LOT-KEEP','created','admin','{}','t0');
INSERT INTO lot_events VALUES(2,'LOT-GONE','approval','q1','{}','t1');
INSERT INTO approvals VALUES('LOT-GONE','q1','release','孤儿审批','t1');
INSERT INTO approvals VALUES('LOT-KEEP','q1','hold','保留的有效审批','t2');
"""
            )
            db.commit()
            db.close()

            service = ComponentService(path)
            admin = _admin_token(service)
            kept = service.get_lot(admin, "LOT-KEEP")
            self.assertEqual(kept["revision"], 1)
            # 同名批次重建后不继承失败请求的痕迹。
            service.create_lot(admin, "LOT-GONE", "FPGA", "R9", 1)
            events = service.audit(admin, "LOT-GONE")
            self.assertEqual([e["event_type"] for e in events], ["created"])
            # 旧库中真实存在批次的审批被保留。
            row = service.db.execute(
                "SELECT decision,decided_revision FROM approvals WHERE lot_id='LOT-KEEP'"
            ).fetchone()
            self.assertEqual(row["decision"], "hold")
            self.assertEqual(row["decided_revision"], 1)


def _admin_token(service: ComponentService) -> str:
    service.bootstrap_admin()
    return service.auth.login("admin", "component-admin")


if __name__ == "__main__":
    unittest.main()
