"""SQLite 数据层：schema、连接与事务助手。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS departments (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    short   TEXT NOT NULL UNIQUE          -- 部门简称，如 RD / MKT
);

CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    role     TEXT NOT NULL,               -- employee / cleaner / tenant_admin / property
    dept_id  INTEGER REFERENCES departments(id)   -- 保洁与物业可为空
);

CREATE TABLE IF NOT EXISTS fridges (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code     TEXT NOT NULL UNIQUE,        -- 如 F12-A
    floor    TEXT NOT NULL,
    name     TEXT NOT NULL,
    rows     INTEGER NOT NULL,
    cols     INTEGER NOT NULL,
    status   TEXT NOT NULL DEFAULT 'active',   -- active / shutdown
    temp_min REAL NOT NULL DEFAULT 2.0,
    temp_max REAL NOT NULL DEFAULT 8.0
);

CREATE TABLE IF NOT EXISTS slots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fridge_id INTEGER NOT NULL REFERENCES fridges(id),
    row       INTEGER NOT NULL,           -- 层架号，自上而下 1..rows
    col       INTEGER NOT NULL,
    label     TEXT NOT NULL,              -- 如 A-3（层-列）
    blocked   INTEGER NOT NULL DEFAULT 0, -- 维护/泄漏封锁
    UNIQUE (fridge_id, row, col)
);

CREATE TABLE IF NOT EXISTS labels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,      -- 扫码内容，如 LBL-RD-0007
    dept_id    INTEGER NOT NULL REFERENCES departments(id),
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    item_name  TEXT NOT NULL,
    keep_days  INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',  -- active / used / voided
    image_path TEXT,                      -- 本地标签图片
    created_at REAL NOT NULL,
    voided_at  REAL
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label_code      TEXT NOT NULL UNIQUE REFERENCES labels(code),
    dept_id         INTEGER NOT NULL REFERENCES departments(id),
    owner_id        INTEGER NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    fridge_id       INTEGER REFERENCES fridges(id),
    slot_id         INTEGER REFERENCES slots(id),
    status          TEXT NOT NULL,
        -- pending_store 待关门确认 / stored 在架 / expired 待清理 /
        -- retention 临时保留区 / disposed 已处置 / claimed_back 申诉返还 / removed 取回
    keep_days       INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    stored_at       REAL,
    expires_at      REAL,
    retention_until REAL,                 -- 申诉截止（进入保留区时起算）
    batch_id        INTEGER,
    photo_path      TEXT                  -- 清理照片（本地目录）
);

-- 一个格位同一时刻只允许一件有效物品（待确认 / 在架 / 已到期未收走）
CREATE UNIQUE INDEX IF NOT EXISTS one_active_item_per_slot
    ON items(slot_id) WHERE status IN ('pending_store', 'stored', 'expired');

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    type    TEXT NOT NULL,                -- label_created / store_pending / stored / ...
    actor   INTEGER,
    detail  TEXT,
    at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS door_state (   -- 模拟门磁适配器状态
    fridge_id INTEGER PRIMARY KEY REFERENCES fridges(id),
    open      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS temps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fridge_id INTEGER NOT NULL REFERENCES fridges(id),
    value     REAL NOT NULL,
    at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shutdowns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fridge_id  INTEGER NOT NULL REFERENCES fridges(id),
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'open',   -- open / closed
    started_at REAL NOT NULL,
    ended_at   REAL
);

CREATE TABLE IF NOT EXISTS migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shutdown_id INTEGER NOT NULL REFERENCES shutdowns(id),
    from_fridge INTEGER NOT NULL,
    to_fridge   INTEGER NOT NULL,
    moved       INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0,   -- 迁移缺件数
    operator    INTEGER,
    at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (      -- 清理批次
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fridge_id  INTEGER NOT NULL REFERENCES fridges(id),
    cleaner_id INTEGER NOT NULL REFERENCES users(id),
    status     TEXT NOT NULL DEFAULT 'open',   -- open / done
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (      -- 认领申诉
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    dept_id     INTEGER NOT NULL REFERENCES departments(id),
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / approved / rejected
    created_at  REAL NOT NULL,
    resolved_by INTEGER,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS exceptions (   -- 异常登记
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
        -- unlabeled 无标签 / wrong_slot 占错格位 / leak 液体泄漏 /
        -- duplicate_scan 重复扫码 / temp_alert 温度超限 / migration_missing 迁移缺件
    fridge_id   INTEGER,
    slot_id     INTEGER,
    item_id     INTEGER,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / resolved
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    resolved_by INTEGER,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS inspections (  -- 卫生检查
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fridge_id    INTEGER NOT NULL REFERENCES fridges(id),
    inspector_id INTEGER NOT NULL,
    result       TEXT NOT NULL,           -- pass / fail
    notes        TEXT,
    at           REAL NOT NULL
);
"""


class Database:
    """轻量连接工厂：每线程一条连接，写操作用 IMMEDIATE 事务。"""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的复用连接（自动提交模式，事务由 tx() 显式控制）。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self.connect()
            c.isolation_level = None  # autocommit
            self._local.conn = c
        return c

    def tx(self):
        return _Tx(self.conn)


class _Tx:
    """BEGIN IMMEDIATE 事务：进入即拿写锁，配合唯一索引实现格位竞争。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def now() -> float:
    return time.time()
