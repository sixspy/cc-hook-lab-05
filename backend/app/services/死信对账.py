# -*- coding: utf-8 -*-
"""死信匣对账（hook-05 之二，救失败测乙）。

现行 app.services.互证 只点数量：未达阈抬回后「事件死信数」和「死信匣待处置数」
一起减一，数量仍对齐，等于把违规抬回当成合法动作。

本服务做跨表联查，专抓：
- 未达阈抬回：死信失败次数 < 最大重试阈，却已处于「已重投」，且拿不出达阈放行凭证；
- 匣票双态：死信终态与事件状态不一致（匣作废/事件却待投已投递，或匣重投/事件却已作废死信）。

违规只写独立的 ``对账影子`` 表，只能通过 :func:`影子列表` / ``/api/死信/对账影子`` 读，
**绝不 UNION 进正式** ``签名目录.死信列表`` / ``/api/死信`` 集合。
"""
from __future__ import annotations

from app.db import 连接  # noqa: F401  (测试可替换本模块「连接」名)
from app.services.时间 import 此刻
from app.services import 争用闸

BONE = "hook-05-死信自动重投闸"


def _有合法放行凭证(conn, 事件号: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM 重投凭证 WHERE 事件号=? AND 判定='放行' AND 状态 IN ('已出匣','已重投') LIMIT 1",
        (事件号,),
    ).fetchone()
    return bool(row)


def 跑对账() -> dict:
    t = 此刻()
    conn = 连接()
    违规: list[dict] = []
    try:
        阈值 = 争用闸.读阈值(conn)
        rows = conn.execute(
            """
            SELECT d.死信号, d.事件号, d.失败次数, d.状态 AS 匣态,
                   e.状态 AS 事件态
            FROM 死信 d LEFT JOIN 事件 e ON e.事件号 = d.事件号
            """
        ).fetchall()

        for r in rows:
            d = dict(r)
            次数 = int(d.get("失败次数") or 0)
            匣态 = d.get("匣态")
            事件态 = d.get("事件态") or ""
            eid = d["事件号"]

            # 1) 未达阈却被抬成「已重投」，且无达阈放行凭证 → 违规抬回。
            if 匣态 == "已重投" and 次数 < 阈值 and not _有合法放行凭证(conn, eid):
                违规.append({
                    "批次时刻": t,
                    "事件号": eid,
                    "死信号": d["死信号"],
                    "类型": "未达阈抬回",
                    "失败次数": 次数,
                    "阈值": 阈值,
                    "判定": f"非法：失败{次数}/{阈值}次未达阈，不得抬回，对账不认其为合法动作",
                    "详情": f"匣态={匣态} 事件态={事件态}",
                })

            # 2) 匣票双态：终态与事件态分裂。
            双态 = (
                (匣态 == "已作废" and 事件态 != "已作废")
                or (匣态 == "已重投" and 事件态 in ("已作废", "死信", ""))
            )
            if 双态:
                违规.append({
                    "批次时刻": t,
                    "事件号": eid,
                    "死信号": d["死信号"],
                    "类型": "匣票双态",
                    "失败次数": 次数,
                    "阈值": 阈值,
                    "判定": f"非法双态：死信匣={匣态}，事件队列={事件态}，未同票",
                    "详情": "自动重投与人工作废并发可致，需同锁 CAS",
                })

        for x in 违规:
            conn.execute(
                "INSERT INTO 对账影子(批次时刻,事件号,死信号,类型,失败次数,阈值,判定,详情)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (x["批次时刻"], x["事件号"], x["死信号"], x["类型"],
                 x["失败次数"], x["阈值"], x["判定"], x["详情"]),
            )
        conn.commit()

        return {
            "骨签": BONE,
            "时刻": t,
            "对齐": len(违规) == 0,
            "是否对齐": len(违规) == 0,
            "是否全部对齐": len(违规) == 0,
            "违规数": len(违规),
            "违规": 违规,
            "影子": 违规,
            "阈值": 阈值,
        }
    finally:
        conn.close()


# 语义别名
对账 = 跑对账


def 影子列表(事件号: str | None = None) -> list[dict]:
    conn = 连接()
    try:
        if 事件号:
            rows = conn.execute(
                "SELECT * FROM 对账影子 WHERE 事件号=? ORDER BY 影子号 DESC", (事件号,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 对账影子 ORDER BY 影子号 DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
