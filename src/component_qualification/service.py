"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
import threading
import uuid

from .analytics import confidence_interval, summarize_signal_profile, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .storage import connect, event, transaction, utcnow


DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}


class ComponentService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)
        # 单个 SQLite 连接被多线程 HTTP 服务共享时的串行化边界。
        self.lock = threading.RLock()

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, unit_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if unit_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValidationFailed("lot fields are invalid")
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO chip_lots(lot_id,product,process_rev,unit_count,status,owner,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (lot_id, product, process_rev, unit_count, "engineering", actor.user_id, now, now),
                )
                event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"lot already exists: {lot_id}") from exc
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"lot not found: {lot_id}")
        return dict(row)

    def _lot_row(self, lot_id: str) -> dict:
        return dict(self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone())

    def add_measurement(self, token: str, lot_id: str, signal_frequency_hz: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise NotFound(f"lot not found: {lot_id}")
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(signal_frequency_hz), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "signal_frequency_hz": signal_frequency_hz})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        lot = self.get_lot(token, lot_id)
        rows = self.db.execute("SELECT signal_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValidationFailed("three measurements are required")
        summary = summarize_signal_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(lot["unit_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "signal_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str, expected_revision: int | None = None) -> dict:
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValidationFailed("decision and reason are required")
        with transaction(self.db):
            actor = self.auth.require(token, "approve")
            row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if row is None:
                raise NotFound(f"lot not found: {lot_id}")
            existing = self.db.execute(
                "SELECT decision,reason FROM approvals WHERE lot_id=? AND reviewer=?",
                (lot_id, actor.user_id),
            ).fetchone()
            if existing is not None:
                if existing["decision"] == decision and existing["reason"] == reason:
                    return self._lot_row(lot_id)
                raise Conflict("reviewer already recorded a different decision for this lot")
            if row["status"] != "engineering":
                raise InvalidState(f"lot status {row['status']} does not allow approval")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise Conflict("lot revision has changed; reload and retry")
            now = utcnow()
            try:
                self.db.execute(
                    "INSERT INTO approvals(lot_id,reviewer,decision,reason,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, actor.user_id, decision, reason, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("approval already recorded for this lot and reviewer") from exc
            cursor = self.db.execute(
                "UPDATE chip_lots SET status=?,updated_at=?,revision=revision+1 WHERE lot_id=? AND status='engineering' AND revision=?",
                (DECISION_STATUS[decision], now, lot_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise Conflict("lot changed concurrently; reload and retry")
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
            return self._lot_row(lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise NotFound(f"lot not found: {lot_id}")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
