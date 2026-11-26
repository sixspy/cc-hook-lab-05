#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-05 收工争用脚本：另起两个登录会话，同时打同一死信的同一把锁。

场景：先正常失败 3 次造一条达阈待处置死信；然后两个会话在栅栏后同时——
  会话甲：POST /api/死信/自动重投
  会话乙：POST /api/死信/作废
断言：
  1) 恰一个会话成功，另一个收到 4xx 争用/已处置；
  2) 匣票同票：匣「已作废」⇔ 事件「已作废」，或匣「已重投」⇔ 事件「待投」；
  3) 绝不出现匣作废/事件待投（已投递）这类双态。
另验：事故夹具（失败 1 次未达阈）点自动重投必须 400 且事件仍「死信」。

用法：
  python scripts/双会话争用.py [http://127.0.0.1:8505] [轮数]
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://127.0.0.1:8505"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 8


def 新会话():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener


def call(opener, method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    安全路径 = urllib.parse.quote(path, safe="/?&=%")
    req = urllib.request.Request(BASE + 安全路径, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def 登录():
    op = 新会话()
    st, _ = call(op, "POST", "/api/登录", {"账号": "admin", "密码": "123456"})
    assert st == 200, f"登录失败 {st}"
    return op


def 造达阈死信(setup, idx: int) -> str:
    eid = f"EVT-RACE-{int(time.time()*1000)}-{idx}"
    st, b = call(setup, "POST", "/api/事件/入队", {
        "事件号": eid, "事件类型": "hook.are.race", "端点键": "ep-billing",
        "载荷摘要": f"race={idx}", "优先级": "紧急",
    })
    assert st == 200, f"入队失败 {st} {b}"
    last = {}
    for _ in range(3):
        st, last = call(setup, "POST", "/api/投递", {"事件号": eid, "强制失败": True})
        assert st == 200, f"投递失败 {st} {last}"
    assert last.get("事件状态") == "死信", f"三次失败后应入死信，实际 {last}"
    return eid


def 一轮(setup, idx: int) -> None:
    eid = 造达阈死信(setup, idx)
    a, b = 登录(), 登录()
    gate = threading.Barrier(2)
    out: dict[str, tuple[int, dict]] = {}

    def auto():
        gate.wait()
        out["auto"] = call(a, "POST", "/api/死信/自动重投", {"事件号": eid})

    def void():
        gate.wait()
        out["void"] = call(b, "POST", "/api/死信/作废", {"事件号": eid, "原因": "双会话争用脚本"})

    t1, t2 = threading.Thread(target=auto), threading.Thread(target=void)
    t1.start(); t2.start(); t1.join(25); t2.join(25)
    assert not t1.is_alive() and not t2.is_alive(), "请求线程超时/死锁"

    sa, ba = out["auto"]
    sv, bv = out["void"]
    a_win = 200 <= sa < 300
    v_win = 200 <= sv < 300
    assert a_win ^ v_win, f"[{eid}] 必须恰一胜，实际 auto={sa}:{ba} void={sv}:{bv}"

    st, detail = call(setup, "GET", f"/api/事件/{urllib.parse.quote(eid)}")
    assert st == 200
    estate = detail["状态"]
    dl = detail.get("死信") or {}
    dstate = dl.get("状态")
    if v_win:
        assert dstate == "已作废" and estate == "已作废", f"匣票双态 {dstate}/{estate}"
    else:
        assert dstate == "已重投" and estate == "待投", f"匣票双态 {dstate}/{estate}"
    print(f"  轮{idx}: auto={sa} void={sv} -> 匣={dstate} 事件={estate}  OK")


def 验未达阈闸(setup) -> None:
    st, fx = call(setup, "POST", "/api/夹具/未达阈死信", {})
    assert st == 200, f"事故夹具失败 {st} {fx}"
    eid = fx["事件号"]
    a = 登录()
    post_st, body = call(a, "POST", "/api/死信/自动重投", {"事件号": eid})
    assert post_st == 400 and body.get("放行") is False, f"未达阈必须 400+放行false，实际 {post_st} {body}"
    assert "未达阈" in body.get("闸口", "")
    st, detail = call(setup, "GET", f"/api/事件/{urllib.parse.quote(eid)}")
    assert st == 200
    assert detail["状态"] == "死信", "未达阈自动重投后事件不得离开死信"
    assert (detail.get("死信") or {}).get("状态") == "待处置", "未达阈不得出匣"
    print(f"  未达阈闸: {eid} -> POST {post_st} {body.get('闸口')}  OK")


def main() -> int:
    print(f"目标 {BASE}，争用 {ROUNDS} 轮")
    setup = 登录()
    验未达阈闸(setup)
    for i in range(ROUNDS):
        一轮(setup, i)
    print("全部通过：同锁恰一胜，匣票无双态，未达阈不出匣。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
