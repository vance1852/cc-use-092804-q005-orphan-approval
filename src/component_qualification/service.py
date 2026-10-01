"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid

from .analytics import confidence_interval, summarize_signal_profile, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .storage import connect, event, transaction, utcnow


DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}
APPROVABLE_STATUSES = {"engineering"}


def _request_digest(decision: str, reason: str) -> str:
    body = json.dumps({"decision": decision, "reason": reason}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ComponentService:
    def __init__(self, database: str = ":memory:", *, cross_thread: bool = False):
        self.db = connect(database, cross_thread=cross_thread)
        self.auth = Auth(self.db)
        # HTTP 线程共享同一连接时，由 API 层持有该锁串行化请求；
        # 多个服务实例（各自连接）之间仍由 SQLite 的 IMMEDIATE 写锁裁决。
        self.gate = threading.RLock()

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
                self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?,1)", (lot_id, product, process_rev, unit_count, "engineering", actor.user_id, now, now))
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
        rows = self.db.execute("SELECT signal_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValidationFailed("three measurements are required")
        summary = summarize_signal_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["unit_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "signal_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(
        self,
        token: str,
        lot_id: str,
        decision: str,
        reason: str,
        expected_revision: int | None = None,
    ) -> dict:
        """在单事务内完成权限、存在性、状态、版本校验和决定写入。

        - 批次不存在 -> NotFound，不写任何记录；
        - 当前状态不可审批 -> InvalidState；
        - expected_revision 过期 -> Conflict；
        - 已有不同决定 -> Conflict；相同决定重放 -> 返回原结果。
        """
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValidationFailed("decision and reason are required")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ValidationFailed("expected_revision must be an integer") from exc
        digest = _request_digest(decision, reason.strip())
        # 权限、存在性、状态、版本、写入全部在同一个 IMMEDIATE 事务内，
        # 任何一步失败都整体回滚，审批、批次状态、审计事件不会留下残片。
        with transaction(self.db):
            actor = self.auth.require(token, "approve")
            lot = self.db.execute(
                "SELECT status,revision FROM chip_lots WHERE lot_id=?", (lot_id,)
            ).fetchone()
            if lot is None:
                raise NotFound(f"lot not found: {lot_id}")
            prior = self.db.execute(
                "SELECT reviewer,decision,reason,expected_revision,decided_revision,request_digest,created_at "
                "FROM approvals WHERE lot_id=?",
                (lot_id,),
            ).fetchone()
            if prior is not None:
                if prior["decision"] == decision:
                    # 相同决定的安全重放：决定已原子生效，返回原结果而不再写入。
                    return self._approval_result(lot_id, prior, replayed=True)
                raise Conflict("a different approval decision already exists for this lot")
            if expected_revision is not None and expected_revision != lot["revision"]:
                raise Conflict(
                    f"stale revision: expected {expected_revision}, current {lot['revision']}"
                )
            if lot["status"] not in APPROVABLE_STATUSES:
                raise InvalidState(f"lot status {lot['status']!r} does not allow approval")
            now = utcnow()
            new_revision = lot["revision"] + 1
            cursor = self.db.execute(
                "UPDATE chip_lots SET status=?,updated_at=?,revision=? "
                "WHERE lot_id=? AND revision=?",
                (DECISION_STATUS[decision], now, new_revision, lot_id, lot["revision"]),
            )
            if cursor.rowcount != 1:
                # 并发修改：让调用方按冲突重试。
                raise Conflict("lot revision changed concurrently")
            self.db.execute(
                "INSERT INTO approvals(lot_id,reviewer,decision,reason,expected_revision,"
                "decided_revision,request_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (lot_id, actor.user_id, decision, reason.strip(), expected_revision,
                 new_revision, digest, now),
            )
            event(self.db, lot_id, "approval", actor.user_id, {
                "decision": decision,
                "reason": reason.strip(),
                "revision": new_revision,
            })
            saved = self.db.execute(
                "SELECT reviewer,decision,reason,expected_revision,decided_revision,request_digest,created_at "
                "FROM approvals WHERE lot_id=?",
                (lot_id,),
            ).fetchone()
            return self._approval_result(lot_id, saved, replayed=False)

    @staticmethod
    def _approval_result(lot_id: str, prior: sqlite3.Row, replayed: bool) -> dict:
        return {
            "lot_id": lot_id,
            "reviewer": prior["reviewer"],
            "decision": prior["decision"],
            "reason": prior["reason"],
            "expected_revision": prior["expected_revision"],
            "revision": prior["decided_revision"],
            "request_sha256": prior["request_digest"],
            "decided_at": prior["created_at"],
            "replayed": replayed,
        }

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        # 外键与事务原子性保证这里只会读到已生效批次的已提交事件。
        return [dict(r) for r in self.db.execute(
            "SELECT e.* FROM lot_events e JOIN chip_lots l ON l.lot_id=e.lot_id "
            "WHERE e.lot_id=? ORDER BY event_id",
            (lot_id,),
        ).fetchall()]
