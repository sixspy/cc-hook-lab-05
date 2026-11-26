# -*- coding: utf-8 -*-
"""争用闸（hook-05 之三）：自动重投 vs 人工作废的同锁串行点。

工程要点：
- 每个处置动作在同一 sqlite 事务里「读死信 → 条件占位(CAS) → 同票改事件/留痕 → commit」。
- 线性化点是条件更新 ``UPDATE 死信 SET 状态=? WHERE 事件号=? AND 状态='待处置'``：
  赢家 rowcount=1；输家在赢家提交后重评 WHERE，rowcount=0，回滚并抛 :class:`争用冲突`。
- 配 busy_timeout，把 SQLite 的 locked/busy 也翻译成同一个争用错误，调用方据此报「已被另一会话处置」。
- 读失败次数与写出匣共用同一连接同一事务，同成同败（由自动重投闸调用）。
"""
from __future__ import annotations

import sqlite3

from app.db import 连接

BONE = "hook-05-死信自动重投闸"


class 争用冲突(ValueError):
    """同一死信被并发处置时的输家异常；是 ValueError，旧路由自动回 400。"""


def 开连() -> sqlite3.Connection:
    conn = 连接()
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


def 读死信(conn: sqlite3.Connection, 事件号: str):
    # SQL 文本被 tests/test_hook_are_3.py 的连接代理当作会合点，勿随意改写格式。
    return conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()


def 读阈值(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT 值 FROM 签名规 WHERE 键='最大重试'").fetchone()
    try:
        return int(row["值"]) if row else 3
    except (TypeError, ValueError):
        return 3


def 占位处置(
    conn: sqlite3.Connection,
    事件号: str,
    新状态: str,
    闸口: str = "",
    出匣态: str = "",
    凭证号: str = "",
) -> None:
    """CAS 线性化点：只有仍是「待处置」才允许改成终态。rowcount!=1 即争用输家。"""
    cur = conn.execute(
        "UPDATE 死信 SET 状态=?, 闸口=?, 出匣态=?, 凭证号=? "
        "WHERE 事件号=? AND 状态='待处置'",
        (新状态, 闸口, 出匣态, 凭证号, 事件号),
    )
    if cur.rowcount != 1:
        raise 争用冲突("争用失败：该死信匣已被另一会话处置，请刷新后核对，同锁只准一个终态")


def 守卫闸口读出(conn: sqlite3.Connection, 事件号: str, 闸口: str, 出匣态: str) -> None:
    """未达阈不出匣，但要把中文闸口落到行上；同一条待处置条件兼做并发守卫。"""
    cur = conn.execute(
        "UPDATE 死信 SET 闸口=?, 出匣态=? WHERE 事件号=? AND 状态='待处置'",
        (闸口, 出匣态, 事件号),
    )
    if cur.rowcount != 1:
        raise 争用冲突("争用失败：死信匣状态已被另一会话改动，请刷新核对")


def 翻译锁错(exc: BaseException) -> Exception:
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return 争用冲突("数据库同锁争用（database is locked），另一会话正在处置同一死信")
    return exc
