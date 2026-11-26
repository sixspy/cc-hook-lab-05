# -*- coding: utf-8 -*-
"""hook-05 争用失败测：自动重投与人工作废并发，不得双态/双胜。

用连接代理在两条真实服务事务的「SELECT 死信」之后设会合点，确定性地逼出
「两边都读到待处置、再各自无条件回写」的竞态。基线无同锁/CAS，两边都成功，
于是同一死信既被重投又被作废（双裁决/双留痕，极端交错还会匣票分裂）。
未改业务服务前本文件必须 pytest 见红。

救绿服务：app.services.争用闸（同锁 + 条件占位 CAS：WHERE 状态='待处置'，
输家 rowcount=0 回滚并报争用，绝不出双态）。
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_are_3_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as dbmod  # noqa: E402
from app import seed  # noqa: E402
from app.db import 连接 as 真连接  # noqa: E402
from app.services import 死信, 投递  # noqa: E402

EID = "EVT-ARE-3-争用"
DID = "DL-ARE-3-争用"


@pytest.fixture()
def 库(tmp_path, monkeypatch):
    p = tmp_path / "are3.db"
    monkeypatch.setattr(dbmod, "库路径", p)
    seed.初始化库()
    return p


def 造达阈死信() -> None:
    """争用测要让重投闸放行，故造失败次数已达阈(3)的待处置死信。"""
    t = "2026-11-25T20:20:00Z"
    conn = 真连接()
    try:
        conn.execute(
            "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (EID, "hook.are.race", "ep-billing", "are=3", "紧急", "死信", t, t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时)"
            " VALUES (?,?,?,?,?,?,?)",
            (DID, EID, "ep-billing", 3, "重试耗尽", "待处置", t),
        )
        conn.commit()
    finally:
        conn.close()


class 会合:
    """两边都读完死信后再同时放行，确定性制造陈旧读竞态。"""

    def __init__(self, 人数: int = 2):
        self.人数 = 人数
        self.计数 = 0
        self.锁 = threading.Lock()
        self.放行 = threading.Event()

    def 到达(self) -> None:
        with self.锁:
            self.计数 += 1
            if self.计数 >= self.人数:
                self.放行.set()
        assert self.放行.wait(10), "会合等待超时（可能被锁串行化后未再读到死信）"


class 假游标:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row is not None else []

    def close(self):
        pass


class 连接代理:
    """只在首次命中死信 SELECT 时，把行取实、释放读锁，再卡会合点。"""

    def __init__(self, real, gate: 会合):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "_armed", True)

    def execute(self, sql, params=None):
        real = object.__getattribute__(self, "_real")
        gate = object.__getattribute__(self, "_gate")
        armed = object.__getattribute__(self, "_armed")
        cur = real.execute(sql) if params is None else real.execute(sql, params)
        s = "".join(str(sql).split())
        if armed and "FROM死信WHERE事件号=?" in s:
            object.__setattr__(self, "_armed", False)
            row = cur.fetchone()
            try:
                cur.close()
            except Exception:
                pass
            gate.到达()
            return 假游标(row)
        return cur

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def 装上连接代理(monkeypatch, gate: 会合) -> None:
    def 工厂(*a, **k):
        return 连接代理(真连接(*a, **k), gate)

    # 基线只有 死信.连接；救绿后闸口/同锁服务各自持有 连接 名，一并装上。
    目标 = [死信]
    for modname in ("app.services.自动重投闸", "app.services.争用闸", "app.services.死信对账"):
        try:
            import importlib

            目标.append(importlib.import_module(modname))
        except Exception:
            pass
    for m in 目标:
        if hasattr(m, "连接"):
            monkeypatch.setattr(m, "连接", 工厂)


def test_争用红_自动重投与作废_不得双胜双态(库, monkeypatch):
    造达阈死信()
    gate = 会合(2)
    装上连接代理(monkeypatch, gate)
    # 中和重投提交后的真实投递，只测「闸 vs 作废」的裁决争用。
    monkeypatch.setattr(投递, "投递一次", lambda *a, **k: {"状态": "成功", "事件状态": "已投递"})

    outcomes: dict[str, str] = {}

    def job(name: str, fn) -> None:
        try:
            fn(EID)
            outcomes[name] = "ok"
        except BaseException as e:  # 输家必须报错，不得静默双胜
            outcomes[name] = f"err:{type(e).__name__}:{e}"

    t1 = threading.Thread(target=job, args=("重投", 死信.重投))
    t2 = threading.Thread(target=job, args=("作废", 死信.作废))
    t1.start(); t2.start()
    t1.join(15); t2.join(15)
    assert not t1.is_alive() and not t2.is_alive(), "争用线程死锁/超时"

    成功 = [k for k, v in outcomes.items() if v == "ok"]
    失败 = {k: v for k, v in outcomes.items() if v != "ok"}
    # 核心断言：同锁只准一个胜者。基线两边都 ok → 见红。
    assert len(成功) == 1, f"自动重投与人工作废必须恰一胜，实际：{outcomes}"
    assert len(失败) == 1
    msg = next(iter(失败.values()))
    assert any(w in msg for w in ("争用", "已处置", "locked", "冲突")), msg

    conn = 真连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (EID,)).fetchone()
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (EID,)).fetchone()
    finally:
        conn.close()

    # 匣票同票：死信终态与事件态必须一致，禁止匣作废而事件待投/已投递这类双态。
    assert dl["状态"] in ("已重投", "已作废")
    if dl["状态"] == "已作废":
        assert ev["状态"] == "已作废", f"匣已作废但事件为{ev['状态']}，双态"
    else:
        assert ev["状态"] in ("待投", "投递中", "已投递"), f"匣已重投但事件为{ev['状态']}，双态"


def test_争用_无代理并发冲击_库内始终单一终态(库, monkeypatch):
    """不设会合点的真实并发冲击：救绿后无论调度如何，匣票都不得分裂。"""
    # 本用例多轮，每轮独立事件；不装读代理，直接打真实同锁。
    monkeypatch.setattr(投递, "投递一次", lambda *a, **k: {"状态": "成功", "事件状态": "已投递"})
    for i in range(6):
        eid = f"EVT-ARE-3-R{i}"
        t = "2026-11-25T20:2{i}:00Z"
        conn = 真连接()
        try:
            conn.execute(
                "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (eid, "hook.are.race", "ep-billing", str(i), "普通", "死信", t, t),
            )
            conn.execute(
                "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"DL-ARE-3-R{i}", eid, "ep-billing", 3, "耗尽", "待处置", t),
            )
            conn.commit()
        finally:
            conn.close()

        start = threading.Barrier(2)
        out: dict[str, str] = {}

        def job(kind: str):
            try:
                start.wait()
                if kind == "re":
                    死信.重投(eid)
                else:
                    死信.作废(eid)
                out[kind] = "ok"
            except BaseException as e:
                out[kind] = f"err:{type(e).__name__}"

        ts = [threading.Thread(target=job, args=(k,)) for k in ("re", "void")]
        for x in ts:
            x.start()
        for x in ts:
            x.join(15)
        assert all(not x.is_alive() for x in ts)

        conn = 真连接()
        try:
            dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (eid,)).fetchone()
            ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (eid,)).fetchone()
        finally:
            conn.close()
        assert dl["状态"] in ("已重投", "已作废"), out
        if dl["状态"] == "已作废":
            assert ev["状态"] == "已作废"
        else:
            assert ev["状态"] in ("待投", "投递中", "已投递")
