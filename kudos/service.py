from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator


class KudosError(Exception):
    status_code = 400


class AuthenticationError(KudosError):
    status_code = 401


class AuthorizationError(KudosError):
    status_code = 403


class NotFoundError(KudosError):
    status_code = 404


class ValidationError(KudosError):
    status_code = 422


class RateLimitError(KudosError):
    status_code = 429

    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class KudosService:
    def __init__(
        self,
        database_path: str,
        submissions_per_hour: int = 10,
        duplicate_window_seconds: int = 60,
    ):
        if submissions_per_hour < 1 or duplicate_window_seconds < 1:
            raise ValueError("Abuse-prevention limits must be positive integers.")
        self.database_path = database_path
        self.submissions_per_hour = submissions_per_hour
        self.duplicate_window_seconds = duplicate_window_seconds
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    roles TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS kudos (
                    kudos_id TEXT PRIMARY KEY,
                    sender_id TEXT,
                    recipient_id TEXT,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_visible INTEGER NOT NULL DEFAULT 1 CHECK (is_visible IN (0, 1)),
                    moderated_by TEXT,
                    moderated_at TEXT,
                    moderation_reason TEXT,
                    deleted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS moderation_audit (
                    audit_id TEXT PRIMARY KEY,
                    kudos_id TEXT NOT NULL,
                    moderator_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('hide', 'restore', 'delete')),
                    reason TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kudos_feed
                    ON kudos(is_visible, deleted_at, created_at DESC, kudos_id DESC);
                CREATE INDEX IF NOT EXISTS idx_kudos_sender_duplicate
                    ON kudos(sender_id, recipient_id, created_at DESC);
                """
            )

    def seed_demo_employees(self) -> None:
        demo_employees = (
            ("E001", "Alex Admin", ("moderator",)),
            ("E002", "Sam Employee", ()),
            ("E003", "Taylor Employee", ()),
        )
        with self.connect() as connection:
            for employee_id, display_name, roles in demo_employees:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO employees
                        (employee_id, display_name, active, roles)
                    VALUES (?, ?, 1, ?)
                    """,
                    (employee_id, display_name, json.dumps(roles)),
                )

    def employee(self, employee_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM employees WHERE employee_id = ? AND active = 1",
                (employee_id,),
            ).fetchone()
        if row is None:
            raise AuthenticationError("An active employee account is required.")
        return row

    def list_employees(self, actor_id: str, query: str = "") -> list[dict]:
        self.employee(actor_id)
        normalized_query = query.strip().casefold()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT employee_id, display_name
                FROM employees
                WHERE active = 1 AND employee_id != ?
                  AND instr(lower(display_name), lower(?)) > 0
                ORDER BY display_name, employee_id
                LIMIT 50
                """,
                (actor_id, normalized_query),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_kudos(
        self, sender_id: str, recipient_id: str, message: str
    ) -> dict:
        sender = self.employee(sender_id)
        if not isinstance(recipient_id, str) or not recipient_id:
            raise ValidationError("Select an active colleague.")
        try:
            recipient = self.employee(recipient_id)
        except AuthenticationError:
            raise ValidationError("Select an active colleague.") from None
        if sender_id == recipient_id:
            raise ValidationError("You cannot give kudos to yourself.")
        if not isinstance(message, str):
            raise ValidationError("Message must be text.")
        clean_message = message.strip()
        if not clean_message:
            raise ValidationError("Message cannot be empty.")
        if len(clean_message) > 500:
            raise ValidationError("Message must be 500 characters or fewer.")

        now = utc_now()
        now_text = timestamp(now)
        cutoff = timestamp(now - timedelta(hours=1))
        duplicate_cutoff = timestamp(
            now - timedelta(seconds=self.duplicate_window_seconds)
        )
        kudos_id = str(uuid.uuid4())

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                """
                SELECT 1 FROM kudos
                WHERE sender_id = ? AND recipient_id = ? AND message = ?
                  AND deleted_at IS NULL AND created_at >= ?
                LIMIT 1
                """,
                (sender_id, recipient_id, clean_message, duplicate_cutoff),
            ).fetchone()
            if duplicate:
                raise RateLimitError(
                    "An identical kudos was submitted to this colleague recently.",
                    retry_after=60,
                )

            count = connection.execute(
                """
                SELECT COUNT(*) FROM kudos
                WHERE sender_id = ? AND deleted_at IS NULL AND created_at >= ?
                """,
                (sender_id, cutoff),
            ).fetchone()[0]
            if count >= self.submissions_per_hour:
                raise RateLimitError("Submission limit reached; try again later.")

            connection.execute(
                """
                INSERT INTO kudos
                    (kudos_id, sender_id, recipient_id, message, created_at, is_visible)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (kudos_id, sender_id, recipient_id, clean_message, now_text),
            )
        return {
            "kudos_id": kudos_id,
            "sender_id": sender_id,
            "sender_name": sender["display_name"],
            "recipient_id": recipient_id,
            "recipient_name": recipient["display_name"],
            "message": clean_message,
            "created_at": now_text,
            "is_visible": True,
        }

    @staticmethod
    def encode_cursor(created_at: str, kudos_id: str) -> str:
        raw = json.dumps([created_at, kudos_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def decode_cursor(cursor: str) -> tuple[str, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            created_at, kudos_id = json.loads(
                base64.urlsafe_b64decode(padded).decode()
            )
            if not isinstance(created_at, str) or not isinstance(kudos_id, str):
                raise ValueError
            return created_at, kudos_id
        except (ValueError, TypeError, json.JSONDecodeError):
            raise ValidationError("Invalid feed cursor.") from None

    def feed(self, actor_id: str, limit: int = 20, cursor: str | None = None) -> dict:
        self.employee(actor_id)
        if not 1 <= limit <= 100:
            raise ValidationError("Limit must be between 1 and 100.")

        query = """
            SELECT k.kudos_id, k.sender_id, s.display_name AS sender_name,
                   k.recipient_id, r.display_name AS recipient_name,
                   k.message, k.created_at, k.is_visible
            FROM kudos AS k
            JOIN employees AS s ON s.employee_id = k.sender_id
            JOIN employees AS r ON r.employee_id = k.recipient_id
            WHERE k.is_visible = 1 AND k.deleted_at IS NULL
              AND s.active = 1 AND r.active = 1
        """
        parameters: list = []
        if cursor:
            created_at, kudos_id = self.decode_cursor(cursor)
            query += " AND (k.created_at < ? OR (k.created_at = ? AND k.kudos_id < ?))"
            parameters.extend((created_at, created_at, kudos_id))
        query += " ORDER BY k.created_at DESC, k.kudos_id DESC LIMIT ?"
        parameters.append(limit + 1)

        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = self.encode_cursor(last["created_at"], last["kudos_id"])
        return {
            "items": [dict(row) for row in visible_rows],
            "next_cursor": next_cursor,
        }

    def moderate(
        self,
        moderator_id: str,
        kudos_id: str,
        action: str,
        reason: str | None = None,
    ) -> dict:
        moderator = self.employee(moderator_id)
        if "moderator" not in json.loads(moderator["roles"]):
            raise AuthorizationError("Moderator role is required.")
        if action not in {"hide", "restore", "delete"}:
            raise ValidationError("Unsupported moderation action.")
        if reason is not None:
            if not isinstance(reason, str):
                raise ValidationError("Moderation reason must be text.")
            reason = reason.strip()
            if len(reason) > 500:
                raise ValidationError("Moderation reason must be 500 characters or fewer.")
            reason = reason or None

        now_text = timestamp(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM kudos WHERE kudos_id = ? AND deleted_at IS NULL",
                (kudos_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Kudos not found.")

            if action == "hide":
                connection.execute(
                    """
                    UPDATE kudos SET is_visible = 0, moderated_by = ?,
                        moderated_at = ?, moderation_reason = ?
                    WHERE kudos_id = ?
                    """,
                    (moderator_id, now_text, reason, kudos_id),
                )
            elif action == "restore":
                connection.execute(
                    """
                    UPDATE kudos SET is_visible = 1, moderated_by = ?,
                        moderated_at = ?, moderation_reason = ?
                    WHERE kudos_id = ?
                    """,
                    (moderator_id, now_text, reason, kudos_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE kudos SET sender_id = NULL, recipient_id = NULL,
                        message = '', is_visible = 0, moderated_by = ?,
                        moderated_at = ?, moderation_reason = ?, deleted_at = ?
                    WHERE kudos_id = ?
                    """,
                    (moderator_id, now_text, reason, now_text, kudos_id),
                )

            connection.execute(
                """
                INSERT INTO moderation_audit
                    (audit_id, kudos_id, moderator_id, action, reason, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), kudos_id, moderator_id, action, reason, now_text),
            )
        return {"kudos_id": kudos_id, "action": action, "occurred_at": now_text}

    def moderation_queue(self, moderator_id: str, limit: int = 50) -> list[dict]:
        moderator = self.employee(moderator_id)
        if "moderator" not in json.loads(moderator["roles"]):
            raise AuthorizationError("Moderator role is required.")
        if not 1 <= limit <= 100:
            raise ValidationError("Limit must be between 1 and 100.")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT k.kudos_id, k.sender_id, s.display_name AS sender_name,
                       k.recipient_id, r.display_name AS recipient_name,
                       k.message, k.created_at, k.is_visible, k.moderated_at,
                       k.moderated_by, k.moderation_reason
                FROM kudos AS k
                JOIN employees AS s ON s.employee_id = k.sender_id
                JOIN employees AS r ON r.employee_id = k.recipient_id
                WHERE k.is_visible = 0 AND k.deleted_at IS NULL
                ORDER BY k.moderated_at DESC, k.kudos_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
