import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout 让并发写入排队而不是立刻抛 "database is locked"
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_tx(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，处置只能有一笔落库。"""
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dependent_zone TEXT NOT NULL UNIQUE,
  upstream_zone TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dispositions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dependency_id INTEGER NOT NULL,
  dependent_batch_id INTEGER NOT NULL,
  upstream_batch_id INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('rollback','quarantine')),
  voucher TEXT NOT NULL UNIQUE,
  note TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  dependent_version_before INTEGER NOT NULL,
  silver_status_after TEXT NOT NULL,
  upstream_status_after TEXT NOT NULL,
  upstream_open_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(dependent_batch_id, dependent_version_before),
  FOREIGN KEY(dependency_id) REFERENCES zone_dependencies(id),
  FOREIGN KEY(dependent_batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(upstream_batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS orchestration_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dependent_batch_id INTEGER NOT NULL UNIQUE,
  upstream_batch_id INTEGER NOT NULL,
  stage TEXT NOT NULL DEFAULT '依赖门禁',
  status TEXT NOT NULL CHECK (status IN ('待处置','已回滚','已隔离放行')),
  disposition_id INTEGER,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(dependent_batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(upstream_batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(disposition_id) REFERENCES dispositions(id)
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]

# 银区闭合必须等上游铜区齐套；种子故意留成"银已闭、铜未齐"的缺口。
SEED_DEPENDENCIES = [
    ("silver_orders", "bronze_orders", "银区闭合依赖铜区齐套"),
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _latest_batch(conn: sqlite3.Connection, zone_code: str):
    return conn.execute(
        "SELECT * FROM landing_batches WHERE zone_code=? "
        "ORDER BY biz_date DESC, id DESC LIMIT 1",
        (zone_code,),
    ).fetchone()


def _seed_dependencies(conn: sqlite3.Connection) -> None:
    for dependent, upstream, label in SEED_DEPENDENCIES:
        conn.execute(
            "INSERT OR IGNORE INTO zone_dependencies(dependent_zone, upstream_zone, label) "
            "VALUES (?,?,?)",
            (dependent, upstream, label),
        )
        # 兼容基线 meta 键
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?,?)",
            (f"dep_{dependent}", upstream),
        )
    for dependent, upstream, _ in SEED_DEPENDENCIES:
        silver = _latest_batch(conn, dependent)
        bronze = _latest_batch(conn, upstream)
        if not silver or not bronze:
            continue
        conn.execute(
            "INSERT INTO orchestration_runs("
            "dependent_batch_id, upstream_batch_id, stage, status, detail) "
            "SELECT ?,?, '依赖门禁','待处置',? "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM orchestration_runs WHERE dependent_batch_id=?"
            ")",
            (
                silver["id"],
                bronze["id"],
                "银区已闭合但铜区依赖仍在收集，解析链路在依赖门禁提前掐断",
                silver["id"],
            ),
        )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        # 老库迁移：landing_batches 补 version
        _ensure_column(conn, "landing_batches", "version", "version INTEGER NOT NULL DEFAULT 1")
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for zone, name, biz, status in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                    "VALUES (?,?,?,?,?)",
                    (zone, name, biz, status, 2 if status == "已闭合" else None),
                )
            closed = conn.execute(
                "SELECT id FROM landing_batches WHERE status='已闭合'"
            ).fetchone()
            if closed:
                bid = closed["id"]
                conn.execute(
                    "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                    "VALUES (?,?,?,?,?)",
                    (bid, "part-000.parquet", "hash-silver-a", 1024, "已闭合"),
                )
                conn.execute(
                    "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                    "VALUES (?,?,?,?,?)",
                    (bid, "part-001.parquet", "hash-silver-b", 2048, "已闭合"),
                )
                conn.execute(
                    "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
                    (bid, 2, "种子闭合"),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # 依赖与编排种子每次都补，保证老库也能长出依赖卡片
        _seed_dependencies(conn)
