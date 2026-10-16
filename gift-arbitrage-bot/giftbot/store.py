"""SQLite: какие лоты уже видели и какие сделки нашли."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .evaluator import Deal
from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    market TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    price TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (market, listing_id, price)
);
CREATE TABLE IF NOT EXISTS deals (
    ts REAL NOT NULL,
    buy_market TEXT NOT NULL,
    sell_market TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    ref_price REAL NOT NULL,
    profit REAL NOT NULL,
    profit_pct REAL NOT NULL,
    anomaly INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    buy_market TEXT NOT NULL,
    sell_market TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    model TEXT,
    number INTEGER,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    expected_profit REAL NOT NULL,
    owned_id TEXT,
    list_price REAL,
    misses INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
"""

# pending — запрос на покупку отправлен; bought — куплено; listed — выставлено;
# unknown — неизвестно, прошла ли покупка; sold — продано; closed — закрыто вручную;
# failed — площадка отказала; dry — тестовая покупка
OPEN_STATUSES = ("pending", "bought", "listed", "unknown")
SPENT_STATUSES = OPEN_STATUSES + ("sold", "closed")

STATUS_NAMES = {
    "pending": "покупается",
    "bought": "куплено",
    "listed": "выставлено",
    "unknown": "неизвестно",
    "sold": "продано",
    "closed": "закрыто",
    "failed": "не куплено",
    "dry": "тест",
}


@dataclass
class Position:
    id: int
    ts: float
    updated: float
    status: str
    buy_market: str
    sell_market: str
    listing_id: str
    collection: str
    model: str | None
    number: int | None
    price: float
    cost: float
    expected_profit: float
    owned_id: str | None
    list_price: float | None
    misses: int
    note: str | None

    def title(self) -> str:
        text = self.collection
        if self.model:
            text += f" — {self.model}"
        if self.number is not None:
            text += f" #{self.number}"
        return text


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        self._clock = clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA)

    def is_new(self, listing: Listing) -> bool:
        """Отмечает лот увиденным. True — видим впервые (или у него новая цена)."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
            (listing.market, listing.listing_id, f"{listing.price:.6f}", self._clock()),
        )
        self._db.commit()
        return cur.rowcount == 1

    def mark_seen(self, listings: Iterable[Listing]) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
            [(i.market, i.listing_id, f"{i.price:.6f}", self._clock()) for i in listings],
        )
        self._db.commit()

    def add_deal(self, deal: Deal, anomaly: bool) -> None:
        self._db.execute(
            "INSERT INTO deals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock(),
                deal.listing.market,
                deal.sell_market,
                deal.listing.listing_id,
                deal.listing.title(),
                deal.listing.price,
                deal.ref_price,
                deal.profit,
                deal.profit_pct,
                int(anomaly),
            ),
        )
        self._db.commit()

    def deals_since(self, seconds: float) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM deals WHERE ts >= ? AND anomaly = 0", (self._clock() - seconds,)
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------ позиции автопокупки

    def open_position(self, deal: Deal, status: str) -> int:
        item = deal.listing
        now = self._clock()
        cur = self._db.execute(
            """INSERT INTO positions (ts, updated, status, buy_market, sell_market, listing_id, collection,
                   model, number, price, cost, expected_profit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, now, status, item.market, deal.sell_market, item.listing_id, item.collection, item.model,
             item.number, item.price, deal.cost, deal.profit),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def update_position(self, position_id: int, **fields) -> None:
        allowed = {"status", "owned_id", "list_price", "misses", "note", "expected_profit"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"неизвестные поля позиции: {unknown}")
        fields["updated"] = self._clock()
        sql = ", ".join(f"{name} = ?" for name in fields)
        self._db.execute(f"UPDATE positions SET {sql} WHERE id = ?", (*fields.values(), position_id))
        self._db.commit()

    def get_position(self, position_id: int) -> Position | None:
        row = self._db.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
        return Position(*row) if row else None

    def positions(self, statuses: Iterable[str]) -> list[Position]:
        statuses = tuple(statuses)
        marks = ",".join("?" * len(statuses))
        rows = self._db.execute(f"SELECT * FROM positions WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
        return [Position(*row) for row in rows]

    def spent_since(self, seconds: float, statuses: Iterable[str]) -> tuple[float, int]:
        """Сколько потрачено и сколько покупок за последние seconds секунд."""
        statuses = tuple(statuses)
        marks = ",".join("?" * len(statuses))
        row = self._db.execute(
            f"SELECT COALESCE(SUM(cost), 0), COUNT(*) FROM positions WHERE ts >= ? AND status IN ({marks})",
            (self._clock() - seconds, *statuses),
        ).fetchone()
        return float(row[0]), int(row[1])

    def prune(self, older_than: float = 3 * 86400) -> None:
        self._db.execute("DELETE FROM seen WHERE ts < ?", (self._clock() - older_than,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
