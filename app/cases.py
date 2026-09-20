"""核对案件的 SQLite 持久化。

每个案件保存三份文本、锚点、裁决列表、当前裁决结果与递增 revision。
追加裁决在同一事务中完成版本校验、合并与落库:版本不符或裁决校验
失败时事务回滚,不产生任何写入;成功后 revision 加一。库文件路径由
环境变量 CASES_DB_PATH 指定(默认 ./cases.db),Compose 下指向数据卷,
容器重启后案件仍可查询。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id   TEXT PRIMARY KEY,
    texts     TEXT NOT NULL,      -- JSON:三段原始文本
    anchors   TEXT NOT NULL,      -- JSON:[[i, j, k], ...]
    decisions TEXT NOT NULL,      -- JSON:[{signature, source, replacement}, ...]
    result    TEXT NOT NULL,      -- JSON:当前裁决后的完整结果
    revision  INTEGER NOT NULL
);
"""


class RevisionConflict(Exception):
    """expected_revision 与库中当前 revision 不符;携带当前版本号。"""

    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(f"revision 冲突:当前为 {current_revision}")


def _db_path() -> str:
    return os.environ.get("CASES_DB_PATH", "cases.db")


def _connect() -> sqlite3.Connection:
    # 每次操作独立连接(无线程共享问题);busy_timeout 等待并发写者提交
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(_SCHEMA)
    return conn


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row_to_case(row: tuple) -> dict:
    return {
        "case_id": row[0],
        "texts": json.loads(row[1]),
        "anchors": json.loads(row[2]),
        "decisions": json.loads(row[3]),
        "result": json.loads(row[4]),
        "revision": row[5],
    }


def create_case(*, texts: list, anchors: list, decisions: list, result: dict) -> dict:
    """新建案件并落库,初始 revision 为 1;返回完整案件。"""
    case_id = uuid.uuid4().hex
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO cases (case_id, texts, anchors, decisions, result, revision)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            (case_id, _dumps(texts), _dumps(anchors), _dumps(decisions), _dumps(result)),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_case(case_id)


def get_case(case_id: str) -> dict | None:
    """按 ID 查询案件;不存在返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT case_id, texts, anchors, decisions, result, revision"
            " FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_case(row) if row else None


def append_decisions(case_id: str, expected_revision: int, apply) -> dict | None:
    """在同一事务中校验版本、合并裁决并落库。

    apply(case) 接收当前案件,返回 (合并后的裁决列表, 重算后的结果);
    其抛出的异常(如裁决冲突)使事务回滚并向上传播,不产生任何写入。
    版本不符抛 RevisionConflict;案件不存在返回 None;成功返回更新后的案件。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT case_id, texts, anchors, decisions, result, revision"
            " FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        case = _row_to_case(row)
        if case["revision"] != expected_revision:
            conn.execute("ROLLBACK")
            raise RevisionConflict(case["revision"])
        merged_decisions, result = apply(case)
        # BEGIN IMMEDIATE 已序列化并发写者,WHERE revision = ? 为防御性校验
        conn.execute(
            "UPDATE cases SET decisions = ?, result = ?, revision = revision + 1"
            " WHERE case_id = ? AND revision = ?",
            (_dumps(merged_decisions), _dumps(result), case_id, expected_revision),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_case(case_id)
