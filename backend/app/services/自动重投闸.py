# -*- coding: utf-8 -*-
"""死信自动重投闸（hook-05 主服务，救失败测甲）。

工程形态（择定）：**独立凭证冻住失败次数指纹，并拒未达阈回刷。**

- 点「自动重投」时，在同一 sqlite 事务里读 ``死信.失败次数`` 与 ``签名规.最大重试``，
  读次数与写出匣**同成同败**。
- 无论放行与否都签发一张独立 ``重投凭证``：把当下失败次数与阈值冻成指纹哈希。
- 未达阈：**不出匣**（死信仍 待处置、事件仍 死信），凭证判「拦截」，并把中文闸口落行；
  事后想拿这张未达阈凭证「回刷出匣」会被 :func:`回刷出匣` 拒绝。
- 达阈：走 :mod:`app.services.争用闸` 的 CAS 占位，死信→已重投、事件→待投 **同票提交**。
"""
from __future__ import annotations

import hashlib
import uuid

from app.db import 连接  # noqa: F401  (测试会替换本模块的「连接」名)
from app.services.时间 import 此刻
from app.services import 争用闸

BONE = "hook-05-死信自动重投闸"

留匣态 = "留匣·未出匣"
出匣态 = "已出匣·抬回待投"


class 闸口未达阈(ValueError):
    """旧「死信重投」动作遇到未达阈时抛的错（路由映射 400）。"""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        super().__init__(verdict.get("闸口") or "失败次数未达阈，不得出匣")


def 指纹哈希(事件号: str, 死信号: str, 失败次数: int, 阈值: int) -> str:
    raw = f"{BONE}|{事件号}|{死信号}|{int(失败次数)}|{int(阈值)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verdict_base(dl, 阈值: int) -> dict:
    次数 = int(dl["失败次数"] or 0)
    达阈 = 次数 >= 阈值
    if 达阈:
        闸口 = f"放行：失败{次数}/{阈值}次已达阈，同票抬回待投"
    else:
        闸口 = f"拦截：失败{次数}/{阈值}次未达阈，留匣禁投"
    return {
        "骨签": BONE,
        "事件号": dl["事件号"],
        "死信号": dl["死信号"],
        "失败次数": 次数,
        "阈值": 阈值,
        "达阈": 达阈,
        "放行": 达阈,
        "同票": True,
        "闸口": 闸口,
    }


def 自动重投(事件号: str) -> dict:
    """评估并执行自动重投闸。

    返回结构化裁决：未达阈也正常返回（``放行=False``），由路由决定 400；
    不在匣/已处置/并发争用才抛 ValueError。
    """
    t = 此刻()
    conn = 争用闸.开连()
    try:
        dl = 争用闸.读死信(conn, 事件号)
        if not dl:
            raise ValueError("不在死信匣")
        if dl["状态"] != "待处置":
            raise ValueError(f"死信已处置（{dl['状态']}），不可自动重投")

        阈值 = 争用闸.读阈值(conn)
        v = _verdict_base(dl, 阈值)
        v["时刻"] = t
        凭证号 = f"CRED-{uuid.uuid4().hex[:12]}"
        fp = 指纹哈希(事件号, dl["死信号"], v["失败次数"], 阈值)
        v["凭证号"] = 凭证号
        v["指纹哈希"] = fp

        if not v["达阈"]:
            # 未达阈：只写「拦截凭证 + 闸口读出」，绝不改 死信.状态 / 事件.状态。
            争用闸.守卫闸口读出(conn, 事件号, v["闸口"], 留匣态)
            conn.execute(
                "INSERT INTO 重投凭证(凭证号,事件号,死信号,失败次数指纹,阈值,指纹哈希,"
                "判定,闸口,状态,同票,骨签,时刻,详情) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (凭证号, 事件号, dl["死信号"], v["失败次数"], 阈值, fp,
                 "拦截", v["闸口"], "留匣拦截", 1, BONE, t, "未达阈，冻指纹拒回刷"),
            )
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "自动重投拦截", 事件号, v["闸口"]),
            )
            conn.commit()
            v.update({"出匣态": 留匣态, "事件态": "死信"})
            return v

        # 达阈：CAS 占位，死信出匣 与 事件回待投 必须同票。
        争用闸.占位处置(conn, 事件号, "已重投", v["闸口"], 出匣态, 凭证号)
        cur = conn.execute(
            "UPDATE 事件 SET 状态='待投', 更新时=? WHERE 事件号=? AND 状态='死信'",
            (t, 事件号),
        )
        if cur.rowcount != 1:
            # 匣票不齐（事件已不是死信）→ 整票回滚，CAS 出匣一并撤销。
            raise ValueError("匣票不同票：事件状态不是死信，已回滚本次出匣")
        conn.execute(
            "INSERT INTO 重投凭证(凭证号,事件号,死信号,失败次数指纹,阈值,指纹哈希,"
            "判定,闸口,状态,同票,骨签,时刻,详情) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (凭证号, 事件号, dl["死信号"], v["失败次数"], 阈值, fp,
             "放行", v["闸口"], "已出匣", 1, BONE, t, "达阈放行，死信匣与事件队列同票"),
        )
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "死信自动重投", 事件号, f"{v['闸口']}；凭证 {凭证号}"),
        )
        conn.commit()
        v.update({"出匣态": 出匣态, "事件态": "待投"})
        return v
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        raise 争用闸.翻译锁错(e) from e if isinstance(e, Exception) else e
    finally:
        conn.close()


def 回刷出匣(凭证号: str) -> dict:
    """拿独立凭证要求回刷出匣。未达阈/指纹不符一律拒绝，且不碰正式死信状态。"""
    t = 此刻()
    conn = 连接()
    try:
        c = conn.execute("SELECT * FROM 重投凭证 WHERE 凭证号=?", (凭证号,)).fetchone()
        if not c:
            raise ValueError("无此重投凭证")
        expect = 指纹哈希(c["事件号"], c["死信号"], int(c["失败次数指纹"]), int(c["阈值"]))
        if expect != c["指纹哈希"]:
            conn.execute(
                "UPDATE 重投凭证 SET 状态=? WHERE 凭证号=?", ("指纹不符·回刷拒绝", 凭证号)
            )
            conn.commit()
            raise ValueError("凭证失败次数指纹已被破坏，拒绝回刷出匣")
        if c["判定"] != "放行" or int(c["失败次数指纹"]) < int(c["阈值"]):
            conn.execute(
                "UPDATE 重投凭证 SET 状态=? WHERE 凭证号=?", ("出匣回刷被拒", 凭证号)
            )
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "回刷拒绝", c["事件号"], f"凭证 {凭证号} 未达阈，拒未达阈回刷"),
            )
            conn.commit()
            raise ValueError("拒绝未达阈回刷：凭证冻住的失败次数未达阈，不得出匣")

        # 合法放行凭证的幂等补偿：若仍待处置则同票补出匣。
        dl = 争用闸.读死信(conn, c["事件号"])
        if dl and dl["状态"] == "待处置":
            争用闸.占位处置(conn, c["事件号"], "已重投", "凭证回刷放行", 出匣态, 凭证号)
            conn.execute(
                "UPDATE 事件 SET 状态='待投', 更新时=? WHERE 事件号=? AND 状态='死信'",
                (t, c["事件号"]),
            )
        conn.execute("UPDATE 重投凭证 SET 状态='已出匣' WHERE 凭证号=?", (凭证号,))
        conn.commit()
        return {"凭证号": 凭证号, "放行": True, "事件号": c["事件号"]}
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def 凭证列表(事件号: str | None = None) -> list[dict]:
    conn = 连接()
    try:
        if 事件号:
            rows = conn.execute(
                "SELECT * FROM 重投凭证 WHERE 事件号=? ORDER BY 编号 DESC", (事件号,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM 重投凭证 ORDER BY 编号 DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
