from __future__ import annotations
import json
import uuid
from pathlib import Path
from app.db import 连接, 夹具目录
from app.services.时间 import 此刻


def 播种未达阈死信() -> dict:
    """hook-05 事故夹具：造一条「只失败一次、未达阈」却在死信匣待处置的脏死信。

    正常投递链在失败次数 >= 最大重试 时才入匣，造不出这种数据；这里显式种入，
    供「再点自动重投」复现未达阈假重投事故与闸口拦截。
    """
    体 = 读取("未达阈死信样例")
    ev = 体.get("事件") or {}
    dl = 体.get("死信") or {}
    eid = str(ev.get("事件号") or f"EVT-ARE-FIX-{uuid.uuid4().hex[:8]}")
    did = str(dl.get("死信号") or f"DL-ARE-FIX-{uuid.uuid4().hex[:8]}")
    次数 = int(dl.get("失败次数") or 1)
    t = 此刻()
    conn = 连接()
    try:
        if not conn.execute("SELECT 1 FROM 事件 WHERE 事件号=?", (eid,)).fetchone():
            conn.execute(
                "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (eid, ev.get("事件类型", "hook.are.once"), ev.get("端点键", "ep-billing"),
                 ev.get("载荷摘要", "are=once"), ev.get("优先级", "紧急"), "死信", t, t),
            )
        # 幂等：已存在同事件死信则直接保留，不重复插。
        if not conn.execute("SELECT 1 FROM 死信 WHERE 事件号=?", (eid,)).fetchone():
            conn.execute(
                "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,闸口,出匣态,入匣时)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (did, eid, ev.get("端点键", "ep-billing"), 次数,
                 dl.get("末次原因", f"失败{次数}次·未达阈"), "待处置",
                 f"拦截：失败{次数}/3次未达阈，留匣禁投", "在匣未出", t),
            )
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "事故夹具", eid, f"种入失败{次数}次未达阈死信，供自动重投闸验证"),
        )
        conn.commit()
        return {"事件号": eid, "死信号": did, "失败次数": 次数}
    finally:
        conn.close()


def 列表() -> dict:
    files = []
    if 夹具目录.is_dir():
        for p in sorted(夹具目录.glob("*.json")):
            files.append(p.stem)
    return {"夹具": files}


def 读取(name: str) -> dict:
    p = 夹具目录 / f"{name}.json"
    if not p.is_file():
        # try Chinese names
        for q in 夹具目录.glob("*.json"):
            if q.stem == name or name in q.stem:
                p = q
                break
    if not p.is_file():
        raise FileNotFoundError(name)
    return json.loads(p.read_text(encoding="utf-8"))
