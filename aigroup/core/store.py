"""她的数据：一个 SQLite 文件（data/group.db）。键值表 kv + 各模块自己的表（每个模块在自己文件里写 SCHEMA，启动时建表）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:        # WAL：后台整理印象和回消息同时读写也不容易“数据库被锁”
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except ValueError:
            return default

    def set(self, key: str, value: Any):
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE k=?", (key,))
        else:
            self.conn.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def backup(self, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        b = sqlite3.connect(str(dest))
        src = sqlite3.connect(str(self.path))          # 单独开一个连接：可以在别的线程里备份，不卡聊天
        try:
            with b:
                src.backup(b)
        finally:
            src.close()
            b.close()


def ensure_columns(conn, table: str, cols: dict[str, str]):
    """给老数据库补上新加的列（没有才加）：cols 是 列名 → 类型和默认值，比如 {"aliases": "TEXT NOT NULL DEFAULT ''"}。"""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
