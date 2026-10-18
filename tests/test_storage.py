import sqlite3

from cryptography.fernet import Fernet

from smartbazaar.storage import Store


def test_sessions_are_encrypted_and_isolated(tmp_path):
    database = tmp_path / "runtime" / "bot.sqlite3"
    store = Store(str(database), Fernet.generate_key().decode())
    try:
        store.save(
            "customer-a", {"challenge": {"request_id": "r1"}, "session": {"mobile": "9000000001"}}
        )
        store.save("customer-b", {"session": {"mobile": "9000000002"}})

        assert store.get("customer-a")["session"]["mobile"] == "9000000001"
        assert store.get("customer-b")["session"]["mobile"] == "9000000002"
        raw = store.db.execute("SELECT data FROM sessions").fetchall()
        assert all(b"9000000001" not in row[0] and b"9000000002" not in row[0] for row in raw)
        assert store.claim("delivery-1", "customer-a") is True
        assert store.claim("delivery-1", "customer-a") is False
        store.enqueue("event-1", "919000000001", ["safe reply"])
        outbound = store.next_outbound()
        store.sent(outbound[0], "provider-message-1")
        store.delivered("provider-message-1", "failed", 131026)
        assert store.counts()["delivery_errors"] == {131026: 1}
    finally:
        store.close()


def test_existing_outbox_schema_is_migrated_for_safe_delivery_diagnostics(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.execute(
        "CREATE TABLE outbox (id TEXT PRIMARY KEY, data BLOB NOT NULL, state TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, "
        "provider_id TEXT, delivery TEXT, created REAL NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    store = Store(str(database), Fernet.generate_key().decode())
    try:
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(outbox)")}
        assert "delivery_error" in columns
    finally:
        store.close()


def test_structured_outbox_message_is_encrypted_and_can_fall_back_to_text(tmp_path):
    store = Store(str(tmp_path / "cards.sqlite3"), Fernet.generate_key().decode())
    try:
        card = {"type": "product_card", "fallback": "Milk listing"}
        store.enqueue("event-card", "919000000001", [card])
        oid, payload, _ = store.next_outbound()
        assert payload["message"] == card
        assert b"Milk listing" not in store.db.execute("SELECT data FROM outbox").fetchone()[0]
        store.fallback_to_text(oid, "919000000001", "Milk listing")
        _, fallback, _ = store.next_outbound()
        assert fallback == {"to": "919000000001", "text": "Milk listing"}
    finally:
        store.close()


def test_auth_rejection_holds_only_recent_replies_for_bounded_recovery(tmp_path, monkeypatch):
    import smartbazaar.storage as storage

    store = Store(str(tmp_path / 'auth.sqlite3'), Fernet.generate_key().decode())
    try:
        now = 1800000000
        monkeypatch.setattr(storage.time, 'time', lambda: now)
        store.enqueue('auth-event', 'synthetic-recipient', ['Reply'])
        oid, _, _ = store.next_outbound()
        store.defer_auth(oid)
        assert store.next_outbound() is None
        monkeypatch.setattr(storage.time, 'time', lambda: now + 61)
        assert store.next_outbound()[0] == oid
        store.defer_auth(oid)
        monkeypatch.setattr(storage.time, 'time', lambda: now + 901)
        assert store.next_outbound() is None
        assert store.counts()['outbox'] == {'failed': 1}
    finally:
        store.close()
