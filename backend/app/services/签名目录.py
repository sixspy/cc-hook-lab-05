from __future__ import annotations
from app.db import 连接
from app.services import 校验


def 签名规() -> dict:
    conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        return {r["键"]: r["值"] for r in rows}
    finally:
        conn.close()


def 改签名规(body: dict) -> dict:
    conn = 连接()
    try:
        for k, v in body.items():
            键 = 校验.非空("键", k)
            值 = 校验.非空("值", v)
            conn.execute(
                "INSERT INTO 签名规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (键, 值),
            )
        conn.commit()
        return 签名规()
    finally:
        conn.close()


def 概览() -> dict:
    conn = 连接()
    try:
        def c(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "端点数": c("SELECT COUNT(*) FROM 投递端点"),
            "启用端点": c("SELECT COUNT(*) FROM 投递端点 WHERE 启用=1"),
            "事件总数": c("SELECT COUNT(*) FROM 事件"),
            "待投": c("SELECT COUNT(*) FROM 事件 WHERE 状态='待投'"),
            "投递中": c("SELECT COUNT(*) FROM 事件 WHERE 状态='投递中'"),
            "已投递": c("SELECT COUNT(*) FROM 事件 WHERE 状态='已投递'"),
            "死信": c("SELECT COUNT(*) FROM 事件 WHERE 状态='死信'"),
            "尝试数": c("SELECT COUNT(*) FROM 投递尝试"),
            "死信匣": c("SELECT COUNT(*) FROM 死信 WHERE 状态='待处置'"),
            "签名规": 签名规(),
        }
    finally:
        conn.close()


_事件命运 = {
    "待投": "待投：已入队，等待签名投递",
    "投递中": "投递中：已占端点，失败计次并退避重试",
    "待确认": "待确认：现场已留存，待互证结案",
    "已投递": "已投递：签名投递成功，主链结案",
    "死信": "死信：失败达阈入匣，须过自动重投闸或人工作废",
    "已作废": "已作废：终态，不再投递",
}

_投递命运 = {
    "成功": "投递成功：签名校验通过，事件转已投递",
    "失败": "投递失败：计入失败次数，达阈才入死信匣",
}


def _阈值(conn) -> int:
    row = conn.execute("SELECT 值 FROM 签名规 WHERE 键='最大重试'").fetchone()
    try:
        return int(row["值"]) if row else 3
    except (TypeError, ValueError):
        return 3


def _默认出匣态(匣态: str) -> str:
    return {
        "待处置": "在匣未出",
        "已重投": "已出匣·抬回待投",
        "已作废": "已作废出匣",
    }.get(匣态, 匣态 or "在匣未出")


def _默认闸口(匣态: str, 次数: int, 阈值: int) -> str:
    if 匣态 == "待处置":
        if 次数 >= 阈值:
            return f"放行：失败{次数}/{阈值}次已达阈，可自动重投"
        return f"拦截：失败{次数}/{阈值}次未达阈，留匣禁投"
    if 匣态 == "已重投":
        return "已放行出匣：死信匣与事件队列同票抬回待投"
    if 匣态 == "已作废":
        return "人工作废：终态不出匣"
    return f"匣态：{匣态}"


def 事件列表() -> list:
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM 事件 ORDER BY 入队时 DESC").fetchall()]
        for r in rows:
            r["命运"] = _事件命运.get(r.get("状态"), f"当前状态：{r.get('状态')}")
        return rows
    finally:
        conn.close()


def 事件一条(事件号: str) -> dict | None:
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["命运"] = _事件命运.get(d.get("状态"), f"当前状态：{d.get('状态')}")
        d["尝试"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY 投递时 DESC", (事件号,)
        ).fetchall()]
        for a in d["尝试"]:
            a["命运"] = _投递命运.get(a.get("状态"), f"尝试状态：{a.get('状态')}")
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        if dl:
            阈值 = _阈值(conn)
            dld = dict(dl)
            次数 = int(dld.get("失败次数") or 0)
            dld["阈值"] = 阈值
            dld["达阈"] = 次数 >= 阈值
            dld["事件态"] = d.get("状态")
            dld["出匣态"] = dld.get("出匣态") or _默认出匣态(dld.get("状态"))
            if not dld.get("闸口"):
                dld["闸口"] = _默认闸口(dld.get("状态"), 次数, 阈值)
            d["死信"] = dld
        else:
            d["死信"] = None
        return d
    finally:
        conn.close()


def 投递列表() -> list:
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM 投递尝试 ORDER BY 投递时 DESC LIMIT 200"
        ).fetchall()]
        for r in rows:
            r["命运"] = _投递命运.get(r.get("状态"), f"尝试状态：{r.get('状态')}")
        return rows
    finally:
        conn.close()


def 死信列表() -> list:
    conn = 连接()
    try:
        阈值 = _阈值(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM 死信 ORDER BY 入匣时 DESC").fetchall()]
        态 = {r["事件号"]: r["状态"] for r in conn.execute("SELECT 事件号,状态 FROM 事件").fetchall()}
        for d in rows:
            次数 = int(d.get("失败次数") or 0)
            匣态 = d.get("状态")
            d["阈值"] = 阈值
            d["达阈"] = 次数 >= 阈值
            d["事件态"] = 态.get(d["事件号"], "")
            d["出匣态"] = d.get("出匣态") or _默认出匣态(匣态)
            if not d.get("闸口"):
                d["闸口"] = _默认闸口(匣态, 次数, 阈值)
            d["命运"] = d["闸口"]
        return rows
    finally:
        conn.close()


def 端点列表() -> list:
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM 投递端点 ORDER BY 端点键").fetchall()]
        q = {r["端点键"]: r for r in conn.execute(
            "SELECT 端点键, "
            "SUM(CASE WHEN 状态='待投' THEN 1 ELSE 0 END) AS 待投数, "
            "SUM(CASE WHEN 状态='投递中' THEN 1 ELSE 0 END) AS 投递中数 "
            "FROM 事件 GROUP BY 端点键"
        ).fetchall()}
        for r in rows:
            row = q.get(r["端点键"])
            r["待投数"] = int(row["待投数"]) if row and row["待投数"] is not None else 0
            r["投递中数"] = int(row["投递中数"]) if row and row["投递中数"] is not None else 0
            r["队列积压"] = r["待投数"] + r["投递中数"]
            r["名册态"] = "启用接流" if int(r.get("启用") or 0) == 1 else "停用拒入队"
        return rows
    finally:
        conn.close()


def 互证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 互证批次 ORDER BY 批次号 DESC LIMIT 50").fetchall()]
    finally:
        conn.close()
