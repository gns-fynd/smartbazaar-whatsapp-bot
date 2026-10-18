"""Single-process POC state; customer payloads and outbound messages are encrypted."""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, path: str, key: str, ttl: int = 86400):
        self.cipher = Fernet(key.encode())
        self.index_key = key.encode()
        self.ttl = ttl
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                owner TEXT PRIMARY KEY, data BLOB NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS shopping_memory (
                owner TEXT NOT NULL, account TEXT NOT NULL, data BLOB NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(owner, account));
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, data BLOB NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                provider_id TEXT, delivery TEXT, delivery_error INTEGER, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS order_attempts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
                state TEXT NOT NULL, data BLOB NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS payment_links (
                id TEXT PRIMARY KEY, attempt TEXT NOT NULL, data BLOB NOT NULL, expires REAL NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        if "delivery_error" not in columns:
            self.db.execute("ALTER TABLE outbox ADD COLUMN delivery_error INTEGER")
        # Raw inbound text/OTPs are never persisted. Interrupted inputs must be resent.
        self.db.execute("UPDATE events SET state='interrupted' WHERE state='processing'")
        self.db.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
        self.db.commit()
        self.prune()

    def digest(self, value: str) -> str:
        return hmac.new(self.index_key, value.encode(), hashlib.sha256).hexdigest()

    def seal(self, value: dict) -> bytes:
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def unseal(self, value: bytes) -> dict:
        return json.loads(self.cipher.decrypt(value))

    def get(self, owner: str) -> dict:
        row = self.db.execute(
            "SELECT data,updated FROM sessions WHERE owner=?", (owner,)
        ).fetchone()
        if not row or row[1] < time.time() - self.ttl:
            self.delete(owner)
            return {}
        return self.unseal(row[0])

    def save(self, owner: str, data: dict, *, touch: bool = True):
        """Persist session state; background readers pass touch=False so they never extend expiry."""
        if touch:
            updated = time.time()
        else:
            row = self.db.execute("SELECT updated FROM sessions WHERE owner=?", (owner,)).fetchone()
            if not row:
                return
            updated = row[0]
        self.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (owner, self.seal(data), updated)
        )
        self.db.commit()

    def active_sessions(self, *, active_within: float) -> list[tuple[str, dict]]:
        """Unexpired sessions with a live sign-in and customer activity within ``active_within``
        seconds, for the background keep-alive loop only; it must not be used to extend expiry."""
        now = time.time()
        rows = self.db.execute(
            "SELECT owner,data FROM sessions WHERE updated>=?", (now - self.ttl,)
        ).fetchall()
        result = []
        for owner, data in rows:
            state = self.unseal(data)
            if state.get("session") and now - state.get("last_inbound", 0) <= active_within:
                result.append((owner, state))
        return result

    def delete(self, owner: str):
        self.db.execute("DELETE FROM sessions WHERE owner=?", (owner,))
        self.db.commit()

    def memory_key(self, session: dict) -> str:
        reference = session.get("account_reference")
        if not isinstance(reference, str) or not reference:
            raise ValueError("Verified account reference required")
        application = session.get("application_id", "685945f46c8c7aee3f3af605")
        return self.digest(f"jiomart:{application}:{reference}")

    def get_memory(self, owner: str, account: str) -> dict:
        row = self.db.execute(
            "SELECT data FROM shopping_memory WHERE owner=? AND account=?",
            (owner, account),
        ).fetchone()
        return self.unseal(row[0]) if row else {}

    def save_memory(self, owner: str, account: str, data: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO shopping_memory VALUES (?,?,?,?)",
            (owner, account, self.seal(data), time.time()),
        )
        self.db.commit()

    def forget_memory(self, owner: str, account: str | None = None):
        if account is None:
            self.db.execute("DELETE FROM shopping_memory WHERE owner=?", (owner,))
        else:
            self.db.execute(
                "DELETE FROM shopping_memory WHERE owner=? AND account=?", (owner, account)
            )
        self.db.commit()

    def begin_attempt(self, confirmation: str, owner: str, account: str, data: dict) -> str | None:
        """Persist an order attempt before any checkout call; a token submits once."""
        attempt_id = self.digest("order:" + confirmation)
        now = time.time()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO order_attempts VALUES (?,?,?,?,?,?,?)",
            (attempt_id, owner, account, "submitting", self.seal(data), now, now),
        )
        self.db.commit()
        return attempt_id if cursor.rowcount == 1 else None

    def latest_attempt(self, owner: str, account: str) -> tuple[str, str, dict] | None:
        row = self.db.execute(
            "SELECT id,state,data FROM order_attempts WHERE owner=? AND account=? "
            "ORDER BY created DESC,rowid DESC LIMIT 1",
            (owner, account),
        ).fetchone()
        return (row[0], row[1], self.unseal(row[2])) if row else None

    def get_attempt(self, attempt_id: str) -> tuple[str, str, str, dict] | None:
        row = self.db.execute(
            "SELECT owner,account,state,data FROM order_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        return (row[0], row[1], row[2], self.unseal(row[3])) if row else None

    def watched_attempts(self, max_age: float = 7 * 86400) -> list[tuple[str, str, str, str, dict]]:
        """Orders placed from chat that still need payment or delivery updates."""
        rows = self.db.execute(
            "SELECT id,owner,account,state,data FROM order_attempts "
            "WHERE state IN ('awaiting_payment','placed') AND created>? ORDER BY created",
            (time.time() - max_age,),
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], self.unseal(r[4])) for r in rows]

    def save_payment_link(self, token: str, attempt_id: str, data: dict, expires: float):
        self.db.execute(
            "INSERT OR REPLACE INTO payment_links VALUES (?,?,?,?)",
            (self.digest("pay:" + token), attempt_id, self.seal(data), expires),
        )
        self.db.commit()

    def supersede_unpaid(self, owner: str, account: str, keep: str) -> int:
        """Stop following older unpaid online orders once a newer order is placed."""
        rows = self.db.execute(
            "SELECT id FROM order_attempts WHERE owner=? AND account=? AND state='awaiting_payment' AND id<>?",
            (owner, account, keep),
        ).fetchall()
        for (attempt_id,) in rows:
            self.db.execute("UPDATE order_attempts SET state='superseded',updated=? WHERE id=?", (time.time(), attempt_id))
            self.db.execute("DELETE FROM payment_links WHERE attempt=?", (attempt_id,))
        self.db.commit()
        return len(rows)

    def delete_payment_links(self, attempt_id: str):
        self.db.execute("DELETE FROM payment_links WHERE attempt=?", (attempt_id,))
        self.db.commit()

    def payment_link(self, token: str) -> tuple[str, dict] | None:
        row = self.db.execute(
            "SELECT attempt,data FROM payment_links WHERE id=? AND expires>?",
            (self.digest("pay:" + token), time.time()),
        ).fetchone()
        return (row[0], self.unseal(row[1])) if row else None

    def update_attempt(self, attempt_id: str, state: str, data: dict):
        self.db.execute(
            "UPDATE order_attempts SET state=?,data=?,updated=? WHERE id=?",
            (state, self.seal(data), time.time(), attempt_id),
        )
        self.db.commit()

    def claim(self, event_id: str, owner: str) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
            (self.digest(event_id), owner, "processing", time.time()),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def complete(self, event_id: str):
        self.db.execute("UPDATE events SET state='completed' WHERE id=?", (self.digest(event_id),))
        self.db.commit()

    def enqueue(self, event_id: str, recipient: str, messages: list[str | dict]):
        for index, message in enumerate(messages):
            oid = self.digest(f"{event_id}:{index}")
            payload = {"to": recipient, "text": message} if isinstance(message, str) else {
                "to": recipient,
                "message": message,
            }
            self.db.execute(
                "INSERT OR IGNORE INTO outbox(id,data,state,created) VALUES (?,?,?,?)",
                (oid, self.seal(payload), "pending", time.time()),
            )
        self.db.commit()

    def next_outbound(self):
        self.db.execute(
            "UPDATE outbox SET state='failed' WHERE state='auth_wait' AND created<=?",
            (time.time() - 900,),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT id,data,attempts FROM outbox WHERE state IN ('pending','auth_wait') "
            "AND next_try<=? ORDER BY created,rowid LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not row:
            return None
        self.db.execute(
            "UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row[0],)
        )
        self.db.commit()
        return row[0], self.unseal(row[1]), row[2] + 1

    def sent(self, oid: str, provider_id: str):
        self.db.execute(
            "UPDATE outbox SET state='sent',provider_id=? WHERE id=?",
            (self.digest(provider_id), oid),
        )
        self.db.commit()

    def failed(self, oid: str, attempts: int, retry: bool):
        self.db.execute(
            "UPDATE outbox SET state=?,next_try=? WHERE id=?",
            ("pending" if retry and attempts < 3 else "failed", time.time() + 10 * attempts, oid),
        )
        self.db.commit()

    def defer_auth(self, oid: str):
        """Hold an unsent reply briefly for credential repair; never replay old replies."""
        now = time.time()
        self.db.execute(
            "UPDATE outbox SET state=CASE WHEN created>? THEN 'auth_wait' ELSE 'failed' END, "
            "next_try=? WHERE id=?", (now - 900, now + 60, oid),
        )
        self.db.commit()

    def fallback_to_text(self, oid: str, recipient: str, text: str):
        """Replace a failed rich card with its already-rendered, safe text fallback."""
        payload = {"to": recipient, "text": text[:4000]}
        self.db.execute(
            "UPDATE outbox SET data=?,state='pending',next_try=? WHERE id=?",
            (self.seal(payload), time.time(), oid),
        )
        self.db.commit()

    def delivered(self, provider_id: str, status: str, error_code: int | None = None):
        if status in {"sent", "delivered", "read", "failed"}:
            safe_code = error_code if type(error_code) is int and error_code >= 0 else None
            self.db.execute(
                "UPDATE outbox SET delivery=?, delivery_error=CASE WHEN ?='failed' THEN "
                "COALESCE(?, delivery_error) ELSE delivery_error END WHERE provider_id=?",
                (status, status, safe_code, self.digest(provider_id)),
            )
            self.db.commit()

    def counts(self) -> dict:
        return {
            "events": dict(
                self.db.execute("SELECT state,count(*) FROM events GROUP BY state").fetchall()
            ),
            "outbox": dict(
                self.db.execute("SELECT state,count(*) FROM outbox GROUP BY state").fetchall()
            ),
            "delivery": dict(
                self.db.execute(
                    "SELECT delivery,count(*) FROM outbox WHERE delivery IS NOT NULL GROUP BY delivery"
                ).fetchall()
            ),
            "delivery_errors": dict(
                self.db.execute(
                    "SELECT delivery_error,count(*) FROM outbox "
                    "WHERE delivery_error IS NOT NULL GROUP BY delivery_error"
                ).fetchall()
            ),
        }

    def prune(self):
        self.db.execute(
            "DELETE FROM shopping_memory WHERE updated<?", (time.time() - 180 * 86400,)
        )
        self.db.execute("DELETE FROM sessions WHERE updated<?", (time.time() - self.ttl,))
        for table in ("events", "outbox"):
            self.db.execute(f"DELETE FROM {table} WHERE created<?", (time.time() - 7 * 86400,))
        self.db.execute("DELETE FROM order_attempts WHERE created<?", (time.time() - 30 * 86400,))
        self.db.execute("DELETE FROM payment_links WHERE expires<?", (time.time() - 86400,))
        self.db.commit()

    def close(self):
        self.db.close()
