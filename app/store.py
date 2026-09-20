"""案件持久化:SQLite 保存三段文本、锚点、裁决列表、当前结果与递增 revision。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    texts TEXT NOT NULL,
    anchors TEXT NOT NULL,
    decisions TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class CaseNotFound(Exception):
    """案件 ID 不存在。"""


class RevisionConflict(Exception):
    """expected_revision 与服务端当前版本不符;current 为服务端当前版本号。"""

    def __init__(self, current: int):
        self.current = current
        super().__init__(f"revision 不符:当前为 {current}")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaseStore:
    """基于 SQLite 的案件存储。

    每个操作使用独立连接,可安全用于多线程;追加裁决在单个 BEGIN IMMEDIATE
    事务内完成"读版本 -> 合并裁决 -> 重算 -> 写入",并发写由 SQLite 串行化,
    版本不符或重算失败都会整体回滚,不产生部分写入。
    """

    def __init__(self, path: str):
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None:自动提交,事务由 SQL 显式控制;timeout 即锁等待上限
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _to_record(row: sqlite3.Row) -> dict:
        return {
            "case_id": row["id"],
            "revision": row["revision"],
            "texts": json.loads(row["texts"]),
            "anchors": json.loads(row["anchors"]),
            "decisions": json.loads(row["decisions"]),
            "result": json.loads(row["result"]),
        }

    def create_case(self, *, texts: list[str], anchors: list, result: dict) -> dict:
        """新建案件:revision 从 0 开始,裁决列表为空。返回完整记录。"""
        case_id = uuid.uuid4().hex
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cases (id, revision, texts, anchors, decisions, result,"
                " created_at, updated_at) VALUES (?, 0, ?, ?, '[]', ?, ?, ?)",
                (
                    case_id,
                    self._dump(texts),
                    self._dump(anchors),
                    self._dump(result),
                    now,
                    now,
                ),
            )
        record = self.get_case(case_id)
        assert record is not None
        return record

    def get_case(self, case_id: str) -> dict | None:
        """按 ID 查询案件;不存在返回 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return self._to_record(row) if row is not None else None

    def append_adjudications(
        self,
        case_id: str,
        expected_revision: int,
        new_decisions: list[dict],
        recompute: Callable[[list[str], list, list[dict]], dict],
    ) -> dict:
        """在同一事务内校验版本、合并既有与新增裁决、重算结果并写入,revision 加一。

        recompute(texts, anchors, merged_decisions) 由调用方提供,负责以既有
        重算/原子校验逻辑产出新的当前结果;它抛出的任何异常(如
        AdjudicationConflict)都会导致整体回滚。版本不符抛 RevisionConflict、
        案件不存在抛 CaseNotFound,两者同样不落任何写入。
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT revision, texts, anchors, decisions FROM cases WHERE id = ?",
                    (case_id,),
                ).fetchone()
                if row is None:
                    raise CaseNotFound(case_id)
                if row["revision"] != expected_revision:
                    raise RevisionConflict(row["revision"])
                texts = json.loads(row["texts"])
                anchors = json.loads(row["anchors"])
                merged = json.loads(row["decisions"]) + new_decisions
                result = recompute(texts, anchors, merged)
                conn.execute(
                    "UPDATE cases SET decisions = ?, result = ?,"
                    " revision = revision + 1, updated_at = ?"
                    " WHERE id = ? AND revision = ?",
                    (
                        self._dump(merged),
                        self._dump(result),
                        _utcnow(),
                        case_id,
                        expected_revision,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        record = self.get_case(case_id)
        assert record is not None
        return record
