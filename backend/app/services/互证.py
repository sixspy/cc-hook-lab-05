from __future__ import annotations
from app.db import 连接
from app.services.时间 import 此刻
from app.services import 死信对账


def 跑一次() -> dict:
    t = 此刻()
    conn = 连接()
    try:
        总 = conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"]
        已 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='已投递'").fetchone()["c"]
        死 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='死信'").fetchone()["c"]
        待 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态 IN ('待投','投递中')").fetchone()["c"]
        匣 = conn.execute("SELECT COUNT(*) AS c FROM 死信 WHERE 状态='待处置'").fetchone()["c"]
        数量对齐 = bool(死 == 匣 and 已 + 死 + 待 <= 总)

        # hook-05：数量对齐之外，还要让死信匣对账抓「未达阈抬回 / 匣票双态」。
        审计 = 死信对账.跑对账()
        违规数 = int(审计.get("违规数") or 0)
        对齐 = 1 if (数量对齐 and 违规数 == 0) else 0
        if 对齐:
            说明 = "事件/投递/死信口径对齐，且无未达阈抬回/匣票双态"
        elif not 数量对齐:
            说明 = "死信匣与事件死信数不一致或水位异常"
        else:
            说明 = f"数量虽平，但死信匣对账发现{违规数}起未达阈抬回/匣票双态，不得当合法"

        conn.execute(
            "INSERT INTO 互证批次(时刻,事件总数,已投递数,死信数,待投数,是否对齐,说明) VALUES (?,?,?,?,?,?,?)",
            (t, 总, 已, 死, 待, 对齐, 说明),
        )
        conn.commit()
        return {
            "时刻": t,
            "事件总数": 总,
            "已投递数": 已,
            "死信数": 死,
            "待投数": 待,
            "死信匣待处置": 匣,
            "是否对齐": bool(对齐),
            "是否全部对齐": bool(对齐),
            "说明": 说明,
            "明细": {"总": 总, "已": 已, "死": 死, "待": 待, "匣": 匣,
                     "数量对齐": 数量对齐, "对账违规数": 违规数},
            "死信匣对账": {
                "对齐": 审计["对齐"], "违规数": 违规数, "违规": 审计["违规"],
            },
        }
    finally:
        conn.close()
