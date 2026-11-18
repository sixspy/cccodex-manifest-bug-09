import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写时排队等锁而不是立刻抛 SQLITE_BUSY，保证登记/闭合串行化
    conn.execute("PRAGMA busy_timeout = 5000")
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
def locked_write(db_path: str | None = None):
    """立即写事务：BEGIN IMMEDIATE 抢库写锁。

    登记与闭合共用这把锁。事务内读到的批次态即本次判定的唯一依据
    （读态来源：提交瞬间库内最新批次态），门槛与批次态同源，
    并发“灌数 vs 闭合”在此串行化，杜绝已闭合批双写新对象。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
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

CREATE TABLE IF NOT EXISTS dedup_scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  eligible_count INTEGER NOT NULL,
  duplicate_group_count INTEGER NOT NULL,
  rejected_row_count INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 存储层兜底：可登记门槛与批次态同源（同库同一条批次行）。
-- 仅“收集中”批允许写入新的“已登记”对象；已闭合/已作废批一律拒写，应用层漏判也拦得住。
-- 闭合流程只做 UPDATE、种子数据写“已闭合”行，均不受此 INSERT 触发器限制。
CREATE TRIGGER IF NOT EXISTS trg_landing_objects_gate
BEFORE INSERT ON landing_objects
WHEN
  NEW.status = '已登记'
  AND (SELECT status FROM landing_batches WHERE id = NEW.batch_id) != '收集中'
BEGIN
  SELECT RAISE(ABORT, '批次非收集中，禁止登记新对象（读态来源：提交瞬间库内批次态）');
END;
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
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
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded','1')"
        )
