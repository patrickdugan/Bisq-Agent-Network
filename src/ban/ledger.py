"""Minimal SQLite GTM event counts; never persist payer details or messages."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import re
from .models import currency_code

EVENTS = frozenset({"merchant_interviewed", "merchant_activated", "quote_viewed",
                    "trade_started", "trade_completed_reported", "merchant_retained"})
FIELD = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def record(path: str | Path, event: str, currency: str, channel: str) -> None:
    currency_code(currency)
    if event not in EVENTS or not FIELD.fullmatch(channel):
        raise ValueError("invalid event type or channel")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events (at TEXT NOT NULL, event TEXT NOT NULL, "
                   "currency TEXT NOT NULL, channel TEXT NOT NULL)")
        db.execute("INSERT INTO events (at,event,currency,channel) VALUES (?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), event, currency.upper(), channel))


def metrics(path: str | Path) -> dict:
    if not Path(path).is_file():
        return {"total_events": 0, "by_event": {}, "by_channel": {},
                "caveat": "Operator-reported events; no independently verified trade settlements"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events (at TEXT NOT NULL, event TEXT NOT NULL, "
                   "currency TEXT NOT NULL, channel TEXT NOT NULL)")
        rows = db.execute("SELECT event, channel FROM events").fetchall()
    return {"total_events": len(rows), "by_event": dict(Counter(r[0] for r in rows)),
            "by_channel": dict(Counter(r[1] for r in rows)),
            "caveat": "Operator-reported events; no independently verified trade settlements"}
