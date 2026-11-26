from __future__ import annotations
from app.db import 连接  # noqa: F401  (争用/测试可替换本模块「连接」名)
from app.services.时间 import 此刻
from app.services import 投递, 争用闸, 自动重投闸


def 作废(事件号: str, 原因: str = "人工作废") -> dict:
    t = 此刻()
    conn = 连接()
    try:
        dl = 争用闸.读死信(conn, 事件号)
        if not dl:
            raise ValueError("不在死信匣")
        if dl["状态"] != "待处置":
            raise ValueError("死信已处置")
        # 同锁 CAS：与自动重投竞争时只有一边能占位成功，杜绝双态。
        争用闸.占位处置(
            conn, 事件号, "已作废",
            闸口=f"人工作废：{原因}，终态不出匣",
            出匣态="已作废出匣",
        )
        conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已作废", t, 事件号))
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (t, "死信作废", 事件号, 原因))
        conn.commit()
        row = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        return dict(row)
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        raise 争用闸.翻译锁错(e) from e if isinstance(e, Exception) else e
    finally:
        conn.close()


def 重投(事件号: str) -> dict:
    """旧动作名「死信重投」挂到 hook-05 自动重投闸上：未达阈直接拒，不出匣。"""
    verdict = 自动重投闸.自动重投(事件号)
    if not verdict.get("放行"):
        raise 自动重投闸.闸口未达阈(verdict)
    out = 投递.投递一次(事件号)
    return {**verdict, **out}


def 列表():
    from app.services import 签名目录
    return 签名目录.死信列表()
