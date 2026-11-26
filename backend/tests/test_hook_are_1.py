# -*- coding: utf-8 -*-
"""hook-05 失败测甲：未达阈自动重投，不得把事件抬回待投。

夹具事故：直接造一条「只失败 1 次、低于最大重试阈(默认3)」却已在死信匣且待处置的脏死信，
再点「自动重投」。基线 app.services.死信.重投 不校验失败次数阈，会把它抬回待投——
本文件在未改业务服务前必须 pytest 见红。

救绿服务：app.services.自动重投闸（独立凭证冻住失败次数指纹 + 同票出匣 + 拒未达阈回刷）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_are_1_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as dbmod  # noqa: E402
from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 死信  # noqa: E402

EID = "EVT-ARE-1-未达阈"
DID = "DL-ARE-1-未达阈"
阈值 = 3


@pytest.fixture()
def 库(tmp_path, monkeypatch):
    p = tmp_path / "are1.db"
    monkeypatch.setattr(dbmod, "库路径", p)
    seed.初始化库()
    return p


def 造未达阈死信(eid: str = EID, did: str = DID, 次数: int = 1) -> None:
    """夹具：失败一次、未达阈的死信（正常投递链造不出，正是事故脏数据）。"""
    t = "2026-11-25T20:00:00Z"
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, "hook.are.once", "ep-billing", "are=1", "紧急", "死信", t, t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时)"
            " VALUES (?,?,?,?,?,?,?)",
            (did, eid, "ep-billing", 次数, f"失败{次数}次·未达阈", "待处置", t),
        )
        conn.commit()
    finally:
        conn.close()


def test_甲_事故红_现状重投不认识失败次数阈(库):
    """未改业务前：现状 死信.重投 不抛错、不拦，未达阈也抬回待投（本断言必须红）。"""
    造未达阈死信()

    # 正确行为应当是「拒绝出匣」。基线不校验 → DID NOT RAISE，本测见红。
    with pytest.raises(ValueError):
        死信.重投(EID)

    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (EID,)).fetchone()
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (EID,)).fetchone()
        # 未达阈：死信必须仍在匣待处置，事件必须仍是死信，不得回待投。
        assert dl["状态"] == "待处置"
        assert ev["状态"] == "死信"
    finally:
        conn.close()


def test_甲_闸口服务_冻失败次数指纹_拒未达阈回刷(库):
    """独立凭证方案：凭证冻住失败次数指纹；未达阈签发拦截凭证，且拒绝拿凭证回刷出匣。"""
    闸 = pytest.importorskip("app.services.自动重投闸")
    造未达阈死信()

    verdict = 闸.自动重投(EID)
    assert verdict["放行"] is False
    assert int(verdict["失败次数"]) == 1
    assert int(verdict["阈值"]) == 阈值
    assert "未达阈" in str(verdict["闸口"])
    assert "留匣" in str(verdict["出匣态"]) or "未出匣" in str(verdict["出匣态"])
    assert verdict["事件态"] == "死信"
    assert verdict.get("骨签") == "hook-05-死信自动重投闸"

    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (EID,)).fetchone()
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (EID,)).fetchone()
        assert dl["状态"] == "待处置"
        assert ev["状态"] == "死信"
        cred = conn.execute(
            "SELECT * FROM 重投凭证 WHERE 事件号=? ORDER BY 编号 DESC LIMIT 1", (EID,)
        ).fetchone() if _凭证有序号(conn) else conn.execute(
            "SELECT * FROM 重投凭证 WHERE 事件号=? ORDER BY 凭证号 DESC LIMIT 1", (EID,)
        ).fetchone()
        assert cred is not None, "必须签发独立凭证冻住指纹"
        assert int(cred["失败次数指纹"]) == 1
        assert int(cred["阈值"]) == 阈值
        assert cred["判定"] == "拦截"
        assert cred["指纹哈希"]
        凭证号 = cred["凭证号"]
    finally:
        conn.close()

    # 未达阈凭证想「回刷」把状态写出匣 → 必须拒，且库不变。
    with pytest.raises(ValueError):
        闸.回刷出匣(凭证号)

    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (EID,)).fetchone()
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (EID,)).fetchone()
        assert dl["状态"] == "待处置"
        assert ev["状态"] == "死信"
    finally:
        conn.close()


def _凭证有序号(conn) -> bool:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(重投凭证)").fetchall()]
    return "编号" in cols


def test_甲_http_自动重投_页上能读到次数_出匣态_事件态_闸口(库):
    """点「自动重投」对应的 HTTP 闸口：400 拒绝体里带齐页上要直读的中文字段。"""
    from app.factory import create_app

    造未达阈死信()
    app = create_app()
    c = app.test_client()
    assert c.post("/api/登录", json={"账号": "admin", "密码": "123456"}).status_code == 200

    r = c.post("/api/死信/自动重投", json={"事件号": EID})
    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert body["放行"] is False
    assert int(body["失败次数"]) == 1
    assert int(body["阈值"]) == 阈值
    assert "未达阈" in body["闸口"]
    assert body["出匣态"]
    assert body["事件态"] == "死信"

    # 正式 /api/死信 集合（同粒度加读字段，不混影子行）也要能直接读到中文闸口。
    rows = c.get("/api/死信").get_json()
    row = next(x for x in rows if x["事件号"] == EID)
    assert int(row["失败次数"]) == 1
    assert row["出匣态"]
    assert row["事件态"] == "死信"
    assert "未达阈" in row["闸口"]

    ev = c.get(f"/api/事件/{EID}").get_json()
    assert ev["状态"] == "死信"
