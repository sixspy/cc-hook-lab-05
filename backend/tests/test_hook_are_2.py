# -*- coding: utf-8 -*-
"""hook-05 失败测乙：死信匣对账不得把「未达阈抬回」当成合法动作。

基线 app.services.互证 只点数量：未达阈脏抬回后，死信数与死信匣待处置数一起减一，
数量仍对齐，于是把违规抬回误判成合法。本文件在未改业务服务前必须 pytest 见红。

救绿服务：app.services.死信对账（跨表联查识别未达阈抬回/匣票双态，
违规只写独立「对账影子」读集合，绝不混进正式 /api/死信 集合）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_are_2_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as dbmod  # noqa: E402
from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 互证, 签名目录  # noqa: E402

EID = "EVT-ARE-2-对账"
DID = "DL-ARE-2-对账"


@pytest.fixture()
def 库(tmp_path, monkeypatch):
    p = tmp_path / "are2.db"
    monkeypatch.setattr(dbmod, "库路径", p)
    seed.初始化库()
    return p


def 造未达阈脏抬回(eid: str = EID, did: str = DID) -> None:
    """绕过闸口的既成脏动作：失败 1 次未达阈，却已把死信抬成已重投、事件抬回待投。"""
    t = "2026-11-25T20:10:00Z"
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, "hook.are.recon", "ep-billing", "are=2", "普通", "死信", t, t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时)"
            " VALUES (?,?,?,?,?,?,?)",
            (did, eid, "ep-billing", 1, "失败1次·未达阈", "待处置", t),
        )
        # 事故动作：未达阈也抬回（基线重投/外部脏写会留下的既成事实）
        conn.execute("UPDATE 死信 SET 状态='已重投' WHERE 事件号=?", (eid,))
        conn.execute("UPDATE 事件 SET 状态='待投', 更新时=? WHERE 事件号=?", (t, eid))
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "死信重投", eid, "未达阈抬回待投"),
        )
        conn.commit()
    finally:
        conn.close()


def test_乙_事故红_现行互证把未达阈抬回判成对齐(库):
    """未改业务前：只对数数的互证仍判全部对齐=True，把违规当合法（本断言必须红）。"""
    造未达阈脏抬回()
    rep = 互证.跑一次()
    # 正确行为：对账必须识破未达阈抬回 → 不得再判全部对齐。
    assert rep["是否全部对齐"] is False
    assert rep["是否对齐"] is False


def test_乙_死信对账_记违规_影子读集合不混正式集合(库):
    """独立对账服务：抓未达阈抬回，写对账影子；正式死信集合仍是且只是 死信 表。"""
    对账 = pytest.importorskip("app.services.死信对账")
    造未达阈脏抬回()

    rep = 对账.跑对账()
    assert rep["对齐"] is False
    assert int(rep["违规数"]) >= 1
    hits = [x for x in rep["违规"] if x["事件号"] == EID and "未达阈" in str(x["类型"])]
    assert hits, f"必须有一条未达阈抬回违规：{rep['违规']}"
    assert "未达阈" in hits[0]["判定"]

    # 影子读集合：独立表/独立接口可读。
    shadow = 对账.影子列表(EID)
    assert len(shadow) >= 1
    assert all(x["事件号"] == EID for x in shadow)

    # 正式集合身份：行数/主键与 死信 表完全一致，影子行绝不 UNION 进来。
    official = 签名目录.死信列表()
    conn = 连接()
    try:
        db_ids = [r["死信号"] for r in conn.execute("SELECT 死信号 FROM 死信").fetchall()]
        shadow_count = conn.execute("SELECT COUNT(*) AS c FROM 对账影子").fetchone()["c"]
    finally:
        conn.close()
    assert shadow_count >= 1
    assert sorted(r["死信号"] for r in official) == sorted(db_ids)
    mine = [r for r in official if r["事件号"] == EID]
    assert len(mine) == 1
    assert mine[0]["状态"] == "已重投"  # 正式集合只反映 死信 表本身，不被影子改写

    # HTTP：对账与影子各有其口，正式 /api/死信 不出现影子专属判定行。
    from app.factory import create_app

    c = create_app().test_client()
    c.post("/api/登录", json={"账号": "admin", "密码": "123456"})
    rr = c.post("/api/死信/对账")
    assert rr.status_code == 200
    assert rr.get_json()["对齐"] is False
    sh = c.get("/api/死信/对账影子").get_json()
    assert any(x["事件号"] == EID for x in sh)
    dl = c.get("/api/死信").get_json()
    assert len(dl) == len(db_ids)
