"""Database storage implementation satisfying Store protocol (Role C).

Provides PostgreSQL persistence with automatic table initialization,
connection management, and query helpers for signals, trades, equity, and risk events.
Includes SQLiteStore as a fallback.
"""
import logging
import time
import sqlite3
from typing import List, Dict, Any, Optional

from app.models import Signal, RiskDecision, OrderRequest, OrderResult
from app.interfaces import Store
from app.config import settings

logger = logging.getLogger("app.storage.store")

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False


class PostgresStore(Store):
    """PostgreSQL implementation of the Store protocol."""

    def __init__(
        self,
        dbname: str = settings.DB_NAME,
        user: str = settings.DB_USER,
        password: str = settings.DB_PASSWORD,
        host: str = settings.DB_HOST,
        port: int = settings.DB_PORT,
    ):
        if not PSYCOPG2_AVAILABLE:
            raise ImportError("psycopg2 is required for PostgresStore. Run: pip install psycopg2-binary")

        self.conn_params = {
            "dbname": dbname,
            "user": user,
            "password": password,
            "host": host,
            "port": port,
        }
        self._init_db()

    def _get_connection(self):
        """Create a new database connection."""
        return psycopg2.connect(**self.conn_params)

    def _init_db(self) -> None:
        """Create required tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(32) NOT NULL,
                        side VARCHAR(16) NOT NULL,
                        entry_price DOUBLE PRECISION NOT NULL,
                        stop_loss DOUBLE PRECISION NOT NULL,
                        take_profit DOUBLE PRECISION NOT NULL,
                        reason TEXT,
                        approved BOOLEAN NOT NULL,
                        approved_size DOUBLE PRECISION NOT NULL,
                        ts BIGINT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);

                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(32) NOT NULL,
                        side VARCHAR(16) NOT NULL,
                        size DOUBLE PRECISION NOT NULL,
                        entry_price DOUBLE PRECISION NOT NULL,
                        client_order_id VARCHAR(128),
                        order_id VARCHAR(128),
                        status VARCHAR(64),
                        error TEXT,
                        ts BIGINT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);

                    CREATE TABLE IF NOT EXISTS equity_snapshots (
                        id SERIAL PRIMARY KEY,
                        ts BIGINT NOT NULL,
                        equity DOUBLE PRECISION NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts ASC);

                    CREATE TABLE IF NOT EXISTS risk_events (
                        id SERIAL PRIMARY KEY,
                        event_type VARCHAR(64) NOT NULL,
                        reason TEXT NOT NULL,
                        ts BIGINT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts DESC);

                    CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        level VARCHAR(16) NOT NULL,
                        message TEXT NOT NULL,
                        module VARCHAR(64),
                        ts BIGINT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

                    CREATE TABLE IF NOT EXISTS candles (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(32) NOT NULL,
                        timeframe VARCHAR(16) NOT NULL,
                        ts BIGINT NOT NULL,
                        open DOUBLE PRECISION NOT NULL,
                        high DOUBLE PRECISION NOT NULL,
                        low DOUBLE PRECISION NOT NULL,
                        close DOUBLE PRECISION NOT NULL,
                        volume DOUBLE PRECISION NOT NULL,
                        CONSTRAINT uq_candle UNIQUE (symbol, timeframe, ts)
                    );
                    CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(symbol, timeframe, ts ASC);

                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS reason TEXT;
                """)
            conn.commit()
        logger.info(f"[storage] Connected to PostgreSQL database '{self.conn_params['dbname']}' and initialized tables.")

    def save_signal(self, signal: Signal, decision: RiskDecision) -> None:
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signals (symbol, side, entry_price, stop_loss, take_profit, reason, approved, approved_size, ts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        signal.symbol,
                        signal.side,
                        float(signal.entry_price),
                        float(signal.stop_loss),
                        float(signal.take_profit),
                        signal.reason,
                        bool(decision.approved),
                        float(decision.size),
                        int(signal.ts),
                    ),
                )
            conn.commit()

    def save_trade(self, order: OrderRequest, result: OrderResult, reason: Optional[str] = None) -> None:
        trade_reason = reason or getattr(order, "reason", None) or ""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades (symbol, side, size, entry_price, client_order_id, order_id, status, error, reason, ts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        order.symbol,
                        order.side,
                        float(order.size),
                        float(order.entry_price),
                        order.client_order_id,
                        result.order_id,
                        result.status,
                        result.error,
                        trade_reason,
                        int(time.time()),
                    ),
                )
            conn.commit()

    def save_equity(self, ts: int, equity: float) -> None:
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO equity_snapshots (ts, equity)
                    VALUES (%s, %s)
                    """,
                    (int(ts), float(equity)),
                )
            conn.commit()

    def save_risk_event(self, event_type: str, reason: str, ts: Optional[int] = None) -> None:
        """Record halt, resume, or kill switch events."""
        now = ts if ts is not None else int(time.time())
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO risk_events (event_type, reason, ts)
                    VALUES (%s, %s, %s)
                    """,
                    (event_type, reason, now),
                )
            conn.commit()

    def save_log(self, level: str, message: str, module: str = "", ts: Optional[int] = None) -> None:
        now = ts if ts is not None else int(time.time())
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logs (level, message, module, ts)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (level.upper(), message, module, now),
                )
            conn.commit()

    def get_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_equity_curve(self, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM signals ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_risk_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM risk_events ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM logs ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_latest_equity(self) -> Optional[float]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
                row = cur.fetchone()
                return float(row["equity"]) if row else None

    def save_candles(self, symbol: str, timeframe: str, candles: List[Any]) -> int:
        if not candles:
            return 0
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_values
                records_dict = {}
                for c in candles:
                    c_ts = int(c.ts if hasattr(c, "ts") else c["ts"])
                    records_dict[(symbol, timeframe, c_ts)] = (
                        symbol,
                        timeframe,
                        c_ts,
                        float(c.open if hasattr(c, "open") else c["open"]),
                        float(c.high if hasattr(c, "high") else c["high"]),
                        float(c.low if hasattr(c, "low") else c["low"]),
                        float(c.close if hasattr(c, "close") else c["close"]),
                        float(c.volume if hasattr(c, "volume") else c["volume"]),
                    )
                records = list(records_dict.values())
                if records:
                    execute_values(
                        cur,
                        """
                        INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume)
                        VALUES %s
                        ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET
                            open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume
                        """,
                        records,
                        page_size=2000,
                    )
            conn.commit()
        return len(records)

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        before_ts: Optional[int] = None,
        after_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = %s AND timeframe = %s"
                params: List[Any] = [symbol, timeframe]
                if before_ts is not None:
                    query += " AND ts < %s"
                    params.append(before_ts)
                if after_ts is not None:
                    query += " AND ts > %s"
                    params.append(after_ts)
                query += " ORDER BY ts DESC LIMIT %s"
                params.append(limit)
                cur.execute(query, params)
                rows = cur.fetchall()
                rows.reverse()
                return [dict(r) for r in rows]

    def get_candle_count(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT COUNT(*) as count, MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = %s AND timeframe = %s",
                    (symbol, timeframe),
                )
                row = cur.fetchone()
                return dict(row) if row else {"count": 0, "min_ts": None, "max_ts": None}

    def get_stored_timeframes(self, symbol: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT timeframe, COUNT(*) as count, MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = %s GROUP BY timeframe ORDER BY timeframe",
                    (symbol,),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_latest_candle(self, symbol: str, timeframe: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if timeframe:
                    cur.execute(
                        "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = %s AND timeframe = %s ORDER BY ts DESC LIMIT 1",
                        (symbol, timeframe),
                    )
                else:
                    cur.execute(
                        "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
                        (symbol,),
                    )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_latest_price(self, symbol: str, timeframe: Optional[str] = None) -> Optional[float]:
        c = self.get_latest_candle(symbol, timeframe)
        return float(c["close"]) if c and "close" in c else None

    def clear_all(self) -> None:
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE signals, trades, equity_snapshots, risk_events, logs, candles RESTART IDENTITY")
            conn.commit()


class SQLiteStore(Store):
    """Production-grade SQLite persistence store implementing Store protocol (Role C)."""

    def __init__(self, db_path: str = settings.DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a connection with WAL mode, busy timeout, and row factory."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize SQLite tables and performance indexes."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    reason TEXT,
                    approved INTEGER NOT NULL,
                    approved_size REAL NOT NULL,
                    ts INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    client_order_id TEXT,
                    order_id TEXT,
                    status TEXT,
                    error TEXT,
                    ts INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    equity REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts ASC);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ts INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts DESC);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    module TEXT,
                    ts INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    UNIQUE(symbol, timeframe, ts)
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_sqlite ON candles(symbol, timeframe, ts ASC);")

            try:
                conn.execute("ALTER TABLE trades ADD COLUMN reason TEXT;")
            except Exception:
                pass
            conn.commit()

    def save_signal(self, signal: Signal, decision: RiskDecision) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO signals (symbol, side, entry_price, stop_loss, take_profit, reason, approved, approved_size, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.symbol,
                    signal.side,
                    float(signal.entry_price),
                    float(signal.stop_loss),
                    float(signal.take_profit),
                    signal.reason,
                    1 if decision.approved else 0,
                    float(decision.size),
                    int(signal.ts),
                ),
            )
            conn.commit()

    def save_trade(self, order: OrderRequest, result: OrderResult, reason: Optional[str] = None) -> None:
        trade_reason = reason or getattr(order, "reason", None) or ""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO trades (symbol, side, size, entry_price, client_order_id, order_id, status, error, reason, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.symbol,
                    order.side,
                    float(order.size),
                    float(order.entry_price),
                    order.client_order_id,
                    result.order_id,
                    result.status,
                    result.error,
                    trade_reason,
                    int(time.time()),
                ),
            )
            conn.commit()

    def save_equity(self, ts: int, equity: float) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots (ts, equity) VALUES (?, ?)",
                (int(ts), float(equity)),
            )
            conn.commit()

    def save_risk_event(self, event_type: str, reason: str, ts: Optional[int] = None) -> None:
        now = ts if ts is not None else int(time.time())
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO risk_events (event_type, reason, ts) VALUES (?, ?, ?)",
                (event_type, reason, now),
            )
            conn.commit()

    def save_log(self, level: str, message: str, module: str = "", ts: Optional[int] = None) -> None:
        now = ts if ts is not None else int(time.time())
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO logs (level, message, module, ts) VALUES (?, ?, ?, ?)",
                (level.upper(), message, module, now),
            )
            conn.commit()

    def get_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_equity_curve(self, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_risk_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_latest_equity(self) -> Optional[float]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
            row = cursor.fetchone()
            return float(row["equity"]) if row else None

    def save_candles(self, symbol: str, timeframe: str, candles: List[Any]) -> int:
        if not candles:
            return 0
        with self._get_connection() as conn:
            records_dict = {}
            for c in candles:
                c_ts = int(c.ts if hasattr(c, "ts") else c["ts"])
                records_dict[(symbol, timeframe, c_ts)] = (
                    symbol,
                    timeframe,
                    c_ts,
                    float(c.open if hasattr(c, "open") else c["open"]),
                    float(c.high if hasattr(c, "high") else c["high"]),
                    float(c.low if hasattr(c, "low") else c["low"]),
                    float(c.close if hasattr(c, "close") else c["close"]),
                    float(c.volume if hasattr(c, "volume") else c["volume"]),
                )
            records = list(records_dict.values())
            if records:
                conn.executemany(
                    """
                    INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume
                    """,
                    records,
                )
            conn.commit()
        return len(records)

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        before_ts: Optional[int] = None,
        after_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            query = "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = ? AND timeframe = ?"
            params: List[Any] = [symbol, timeframe]
            if before_ts is not None:
                query += " AND ts < ?"
                params.append(before_ts)
            if after_ts is not None:
                query += " AND ts > ?"
                params.append(after_ts)
            query += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            rows.reverse()
            return [dict(r) for r in rows]

    def get_candle_count(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as count, MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe),
            )
            row = cursor.fetchone()
            return dict(row) if row else {"count": 0, "min_ts": None, "max_ts": None}

    def get_stored_timeframes(self, symbol: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timeframe, COUNT(*) as count, MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = ? GROUP BY timeframe ORDER BY timeframe",
                (symbol,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_latest_candle(self, symbol: str, timeframe: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if timeframe:
                cursor.execute(
                    "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT 1",
                    (symbol, timeframe),
                )
            else:
                cursor.execute(
                    "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                    (symbol,),
                )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_price(self, symbol: str, timeframe: Optional[str] = None) -> Optional[float]:
        c = self.get_latest_candle(symbol, timeframe)
        return float(c["close"]) if c and "close" in c else None

    def clear_all(self) -> None:
        """Clear all tables (useful for unit tests and clean slate)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM signals;")
            conn.execute("DELETE FROM trades;")
            conn.execute("DELETE FROM equity_snapshots;")
            conn.execute("DELETE FROM risk_events;")
            conn.execute("DELETE FROM logs;")
            conn.execute("DELETE FROM candles;")
            conn.commit()


class StoreLoggingHandler(logging.Handler):
    """Logging handler that persists log records to the Store."""

    def __init__(self, store: Store, level=logging.INFO):
        super().__init__(level)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if hasattr(self.store, "save_log"):
                msg = self.format(record)
                self.store.save_log(
                    level=record.levelname,
                    message=msg,
                    module=record.name,
                    ts=int(record.created),
                )
        except Exception:
            self.handleError(record)


def get_default_store() -> Store:
    """Factory creating PostgresStore if configured and reachable, else SQLiteStore."""
    if settings.DB_PASSWORD and PSYCOPG2_AVAILABLE:
        try:
            store = PostgresStore()
            return store
        except Exception as e:
            logger.warning(f"[storage] Could not connect to PostgreSQL ({e}). Falling back to SQLiteStore.")
    return SQLiteStore()
