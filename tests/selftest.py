#!/usr/bin/env python3
"""离线自测：不起外部网络，本机起一个完整实例，逐项断言。

    python3 tests/selftest.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


DATA_DIR = tempfile.mkdtemp(prefix="acn-")
TCP_PORT = free_port()
HTTP_PORT = free_port()
UPSTREAM_PORT = free_port()
os.environ.update({
    "ACN_DATA_DIR": DATA_DIR,
    "ACN_TCP_HOST": "127.0.0.1",
    "ACN_TCP_PORT": str(TCP_PORT),
    "ACN_HTTP_HOST": "127.0.0.1",
    "ACN_HTTP_PORT": str(HTTP_PORT),
    "ACN_ADMIN_PASSWORD": "test-admin",
    "ACN_MANAGER_SECRET": "manager-secret",
    "ACN_PUBLIC_API": "1",
    "ACN_RETENTION_HOURS": "24",
    "ACN_INGEST_ENABLED": "1",
    "ACN_INGEST_HOST": "127.0.0.1",
    "ACN_INGEST_PORT": str(UPSTREAM_PORT),
    "ACN_INGEST_LOGIN": "BG7LZQ",
    "ACN_INGEST_FILTER": "r/35/105/2000",
})

from aprsnetcn import api, aprslib, auth, config, filters, tocalls  # noqa: E402
from aprsnetcn.hub import Hub  # noqa: E402
from aprsnetcn.ingest import Ingest  # noqa: E402
from aprsnetcn.main import self_check  # noqa: E402
from aprsnetcn.store import Store, now_iso  # noqa: E402

PASSED = []
FAILED = []


def ok(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("ok   %s%s" % (name, ("：" + str(detail)) if detail else ""))
    else:
        FAILED.append(name)
        print("FAIL %s%s" % (name, ("：" + str(detail)) if detail else ""))


# ── 纯函数 ──
def test_passcode():
    ok("N0CALL 的 passcode 是 13023", aprslib.passcode("N0CALL") == 13023,
       aprslib.passcode("N0CALL"))
    ok("SSID 不影响 passcode", aprslib.passcode("bg7lzq-9") == aprslib.passcode("BG7LZQ"))


def test_parse():
    pkt = aprslib.parse("BG7LZQ>APRS,TCPIP*:!3954.00N/11623.00E>hello")
    ok("解出源呼号", pkt and pkt.src == "BG7LZQ")
    ok("解出纬度", pkt and abs(pkt.lat - 39.9) < 0.01, pkt and pkt.lat)
    ok("解出经度", pkt and abs(pkt.lon - 116.383) < 0.01, pkt and pkt.lon)
    ok("解出符号", pkt and (pkt.sym_table, pkt.sym_code) == ("/", ">"))
    ok("解出注释", pkt and pkt.comment == "hello")
    pkt = aprslib.parse("BG7LZQ>APRS::BG6TA    :hi{12")
    ok("消息报文类型是 m", pkt and pkt.type == "m")
    pkt = aprslib.parse("BG7LZQ>APRS:}BG6TA>APRS,TCPIP,BG7LZQ*:!3954.00N/11623.00E>")
    ok("第三方报文取内层源", pkt and pkt.src == "BG6TA")
    ok("拆不开的行返回 None", aprslib.parse("garbage") is None)
    ok("带字母 SSID 的呼号认", aprslib.parse("BI1QKA-B>APCHP0:!3954.00N/11623.00E>x") is not None)
    text, error = aprslib.clean_line("BG6TA>APRS:!3954.00N/11623.00E>x 云南", 512)
    ok("注释里的中文不丢", text is not None and "云南" in text, error)


def test_filters():
    pkt = aprslib.parse("BG7LZQ>APRS,TCPIP*:!3954.00N/11623.00E>hello")
    ctx = filters.Context(call="BG6TA")
    flt = filters.Filter.parse(["r/39.9/116.4/300"])
    ok("r/ 命中范围内的台站", flt.match(pkt, ctx))
    flt = filters.Filter.parse(["r/0/0/10"])
    ok("r/ 拦掉范围外的台站", not flt.match(pkt, ctx))
    flt = filters.Filter.parse(["b/BG7LZQ"])
    ok("b/ 按呼号命中", flt.match(pkt, ctx))
    flt = filters.Filter.parse(["t/m"])
    ok("t/ 只放消息报文", not flt.match(pkt, ctx))
    flt = filters.Filter.parse(["p/BG7"])
    ok("p/ 前缀命中", flt.match(pkt, ctx))
    flt = filters.Filter.parse([])
    ok("没有过滤器时全收", flt.match(pkt, ctx))
    try:
        filters.Filter.parse(["z/1"])
        ok("认不出的过滤器被拒", False)
    except filters.FilterError:
        ok("认不出的过滤器被拒", True)


# ── 端到端 ──
class Node:
    def __init__(self):
        self.store = Store(config.DB_PATH)
        self.hub = Hub(self.store)
        self.hub.start()
        self.server, self.api = api.make_server(self.store, self.hub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.hub.stop()
        self.server.shutdown()


def read_until(sock, needle, timeout=4.0):
    sock.settimeout(timeout)
    data = b""
    while needle.encode() not in data:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", "replace")


def connect(node, login_line, needle="logresp"):
    sock = socket.create_connection(("127.0.0.1", TCP_PORT), timeout=5)
    read_until(sock, "aprsnetcn", 3.0)
    sock.sendall((login_line + "\r\n").encode())
    return sock, read_until(sock, needle, 3.0)


def api_call(path, method="GET", body=None, token="", admin=""):
    url = "http://127.0.0.1:%d%s" % (HTTP_PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if admin:
        request.add_header("X-ACN-Token", admin)
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


def test_end_to_end():
    node = Node()
    store = node.store
    store.save_account("BG7LZQ", account="bg7lzq",
                       pass_hash=auth.hash_password("pw123"),
                       status="authorized", note="管理员")
    store.save_account("BG6TA", status="authorized")
    store.save_account("BG0TST", status="pending")

    code = aprslib.passcode("BG7LZQ")
    # ① 账号 + 密码登录
    a, reply = connect(node, "user BG7LZQ pass %d vers APRSLocus 2.2.0 auth bg7lzq pw123 "
                             "filter r/39.9/116.4/300" % code)
    ok("账号密码登录返回 verified", "logresp BG7LZQ verified" in reply, reply.strip())
    more = read_until(a, "active", 2.0)
    ok("过滤器回执送到", "# filter r/39.9/116.4/300 active" in (reply + more),
       (reply + more).strip())
    # ② 不带账号密码：可选模式按 passcode 放行，强制模式拒
    b, reply = connect(node, "user BG7LZQ pass %d" % code)
    ok("可选模式下不带 auth 也能登录", "logresp BG7LZQ verified" in reply, reply.strip()[:90])
    b.close()
    config.AUTH_MODE = "required"
    b, reply = connect(node, "user BG7LZQ pass %d" % code, needle="not authorized")
    ok("强制模式下不带 auth 被拒", "auth-required" in reply, reply.strip())
    b.close()
    config.AUTH_MODE = "optional"
    # ②b 带了错的密码照样拒
    b, reply = connect(node, "user BG7LZQ pass %d auth bg7lzq wrong" % code,
                       needle="not authorized")
    ok("带错的密码被拒", "auth-failed" in reply, reply.strip())
    b.close()
    # ③ 不在名单
    b, reply = connect(node, "user N0CALL pass -1", needle="not authorized")
    ok("不在名单被拒", "not-in-whitelist" in reply, reply.strip())
    b.close()
    # ④ 待审核
    b, reply = connect(node, "user BG0TST pass %d" % aprslib.passcode("BG0TST"),
                       needle="not authorized")
    ok("待审核状态被拒", "(pending)" in reply, reply.strip())
    b.close()
    # ⑤ 只收不发的观察者
    observer, reply = connect(node, "user BG6TA pass %d vers APRSLocus 2.2.0"
                              % aprslib.passcode("BG6TA"))
    ok("正常 passcode 登录返回 verified", "logresp BG6TA verified" in reply, reply.strip())
    read_only, reply = connect(node, "user BG6TA-9 pass -1")
    ok("pass -1 只给收", "unverified" in reply and "logresp BG6TA-9" in reply, reply.strip())
    # ⑥ 过滤器隔离
    far, reply = connect(node, "user BG6TA pass %d filter r/0/0/5"
                         % aprslib.passcode("BG6TA"))
    ok("远端过滤器登录成功", "logresp BG6TA verified" in reply, reply.strip())

    # ⑦ 转发
    time.sleep(0.3)
    a.sendall(b"BG7LZQ>APRS,TCPIP*:!3954.00N/11623.00E>test packet\r\n")
    got = read_until(observer, "test packet", 3.0)
    ok("同一连接收到转发的报文", "test packet" in got, got.strip()[:80])
    ok("转发时补了 qAC", "qAC,BG7LZQ" in got, got.strip()[:80])
    far.settimeout(1.0)
    try:
        leaked = far.recv(4096).decode("utf-8", "replace")
    except socket.timeout:
        leaked = ""
    ok("被过滤器拦住的连接收不到", "test packet" not in leaked)
    # ⑧ 只读连接发报文被挡
    read_only.sendall(b"BG6TA-9>APRS:!3954.00N/11623.00E>nope\r\n")
    reply = read_until(read_only, "read-only", 3.0)
    ok("只读连接发报文被告知丢掉", "read-only" in reply, reply.strip()[:80])

    time.sleep(0.4)
    rows = store.stations()
    ok("24h 列表里有这台站", any(row["call"] == "BG7LZQ" for row in rows),
       [row["call"] for row in rows])
    ok("台站带了位置", all(row["lat"] is not None for row in rows))

    # ⑨ API
    status, health = api_call("/api/v1/health")
    ok("健康检查不用鉴权", status == 200 and health.get("ok"), health.get("stations_24h"))
    status, body = api_call("/api/v1/stations")
    ok("台站列表要 token", status == 401, status)
    status, login = api_call("/api/v1/login", "POST",
                             {"account": "bg7lzq", "password": "pw123"})
    ok("账号密码换到 token", status == 200 and login.get("token"), status)
    token = login.get("token") or ""
    status, login_bad = api_call("/api/v1/login", "POST",
                                 {"account": "bg7lzq", "password": "wrong"})
    ok("密码错拿不到 token", status == 401, status)
    status, body = api_call("/api/v1/stations?window=24h", token=token)
    ok("拿到 24 小时台站", status == 200 and body.get("count", 0) >= 1, body.get("count"))
    ok("台站列表带 total / offset / limit",
       body.get("total", 0) >= body.get("count", 0)
       and body.get("offset") == 0 and body.get("limit") == 500,
       (body.get("total"), body.get("offset"), body.get("limit")))
    status, whole = api_call("/api/v1/stations?window=24h&limit=5000", token=token)
    order = [row.get("call") for row in whole.get("stations") or []]
    walked = []
    for offset in range(min(len(order), 3)):
        status, page = api_call("/api/v1/stations?window=24h&limit=1&offset=%d" % offset,
                                token=token)
        if status != 200 or page.get("count") != 1:
            walked.append(None)
            continue
        walked.append((page.get("stations") or [{}])[0].get("call"))
        if page.get("offset") != offset:
            walked[-1] = None
    ok("一页一条能按顺序走完", walked == order[:len(walked)] and all(walked),
       (walked, order[:len(walked)]))
    if len(order) >= 2:
        status, tail = api_call("/api/v1/stations?window=24h&limit=1&offset=%d"
                                % (len(order) - 1), token=token)
        ok("最后一页也是这一台", (tail.get("stations") or [{}])[0].get("call") == order[-1],
           ((tail.get("stations") or [{}])[0].get("call"), order[-1]))
    status, one = api_call("/api/v1/stations/BG7LZQ", token=token)
    ok("单站带轨迹", status == 200 and len(one.get("track") or []) >= 1,
       len(one.get("track") or []))
    status, missing = api_call("/api/v1/stations/BG0XYZ", token=token)
    ok("查不到的台站返回 404", status == 404, status)
    status, body = api_call("/api/v1/stations?bbox=39,115,41,118", token=token)
    ok("bbox 过滤生效", status == 200 and body.get("count") >= 1, body.get("count"))
    status, body = api_call("/api/v1/stations?bbox=0,0,1,1", token=token)
    ok("bbox 之外的被排除", status == 200 and body.get("count") == 0, body.get("count"))
    status, doc = api_call("/api/v1/openapi.json")
    ok("OpenAPI 文档取得到", status == 200 and doc.get("openapi", "").startswith("3."))

    # ⑨.5 公开台站页与它调的两个接口：不验 token
    status, body = api_call("/api/public/stations?window=24h&limit=5000")
    ok("公开台站列表不用 token", status == 200 and body.get("count", 0) >= 1,
       body.get("count"))
    ok("公开列表按台站聚合", all(row.get("call") for row in body.get("stations") or []),
       len(body.get("stations") or []))
    status, one = api_call("/api/public/stations/BG7LZQ")
    ok("公开单站带轨迹", status == 200 and len(one.get("track") or []) >= 1,
       len(one.get("track") or []))
    status, _ = api_call("/api/public/stations/BG0XYZ")
    ok("公开单站查不到返回 404", status == 404, status)
    status, _ = api_call("/api/public/stations?bbox=1,2,3")
    ok("公开列表参数写错返回 400", status == 400, status)
    page = urllib.request.urlopen("http://127.0.0.1:%d/stations" % HTTP_PORT,
                                  timeout=5).read().decode("utf-8")
    ok("台站页取得到", "<canvas" in page and "24 小时" in page, len(page))
    coast = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/assets/coast.json" % HTTP_PORT,
        timeout=5).read().decode("utf-8"))
    ok("世界轮廓随页发出", len(coast.get("rings") or []) > 10,
       len(coast.get("rings") or []))

    # ⑨.6 台站数据默认不对外：开关关掉就只回 403
    config.PUBLIC_API = False
    try:
        for path in ("/stations", "/api/public/stations?limit=1",
                     "/api/public/stations/BG7LZQ"):
            status, body = api_call(path)
            ok("不对外时 %s 回 403" % path.split("?")[0],
               status == 403 and body.get("error") == "not-public", status)
        status, data = api_call("/api/v1/stations?limit=1", token=token)
        ok("不对外时带 token 的台站接口照常", status == 200 and data.get("count") >= 1,
           status)
        status, ver = api_call("/api/v1/version")
        ok("版本接口报出 public_api 关", ver.get("public_api") is False,
           ver.get("public_api"))
    finally:
        config.PUBLIC_API = True

    # ⑩ 管理接口
    status, state_body = api_call("/api/admin/state", admin="test-admin")
    ok("控制台状态要口令", status == 200 and state_body.get("ok"), status)
    status, _ = api_call("/api/admin/state", admin="wrong")
    ok("错口令被拒", status == 401, status)
    status, saved = api_call("/api/admin/account", "POST",
                             {"call": "bh4hga", "status": "authorized"}, admin="test-admin")
    ok("控制台加账号", status == 200 and saved["account"]["call"] == "BH4HGA", status)
    ok("加账号时名单版本前进", saved.get("version", 0) >= 1, saved.get("version"))

    # ⑩.1 控制台里的台站与账号列
    ok("状态里的账号不把口令哈希发出去",
       all("pass_hash" not in row for row in state_body.get("accounts") or []),
       len(state_body.get("accounts") or []))
    ok("状态里的账号带 has_password 标志",
       all("has_password" in row for row in state_body.get("accounts") or []), "")
    status, listed = api_call("/api/admin/stations?window=24h&limit=10", admin="test-admin")
    ok("控制台台站列表", status == 200 and listed.get("count", 0) >= 1,
       listed.get("count"))
    status, one = api_call("/api/admin/stations/BG7LZQ", admin="test-admin")
    ok("控制台单台站带轨迹", status == 200 and len(one.get("track") or []) >= 1,
       len(one.get("track") or []))
    status, _ = api_call("/api/admin/stations/BG0XYZ", admin="test-admin")
    ok("控制台查不到的台站回 404", status == 404, status)
    status, _ = api_call("/api/admin/stations?limit=1")
    ok("控制台台站列表不带口令回 401", status == 401, status)
    status, whitelist = api_call("/api/admin/whitelist", admin="test-admin")
    ok("导出名单拿得到记录", status == 200 and len(whitelist.get("records") or []) >= 1,
       len(whitelist.get("records") or []))

    # ⑩.2 控制台台站分页与名单汇总
    ok("台站列表带总数与分页参数", "total" in listed and listed.get("limit") == 10
       and listed.get("offset") == 0, sorted(listed))
    ok("台站总数不小于本页条数", listed.get("total", 0) >= listed.get("count", 0),
       (listed.get("total"), listed.get("count")))
    pkt = aprslib.parse("BG6TA>APRS:!3955.00N/11624.00E>paging probe")
    store.record_position(pkt, via="is")
    status, walk = api_call("/api/admin/stations?window=24h&limit=1&offset=1",
                            admin="test-admin")
    ok("台站分页 offset 往后翻", status == 200 and walk.get("count") == 1
       and walk.get("offset") == 1, (walk.get("count"), walk.get("offset")))
    status, allrows = api_call("/api/admin/stations?window=24h&limit=10", admin="test-admin")
    ok("分页的第二台就是列表的第二台",
       (walk.get("stations") or [{}])[0].get("call")
       == (allrows.get("stations") or [{}, {}])[1].get("call"),
       [(row.get("call")) for row in allrows.get("stations") or []])
    summary = (state_body.get("meta") or {}).get("whitelist") or {}
    ok("状态里带名单汇总", summary.get("entries", 0) >= 1, summary.get("entries"))
    ok("名单汇总按状态分好组", sum((summary.get("by_status") or {}).values())
       == summary.get("entries"), summary.get("by_status"))
    ok("名单汇总带同步版本与策略", "version" in summary and "max_age_hours" in summary,
       summary.get("version"))

    # ⑩.3 审计：筛选、搜索、往后翻
    status, _ = api_call("/api/admin/audits")
    ok("审计列表不带口令回 401", status == 401, status)
    status, audits = api_call("/api/admin/audits?limit=5", admin="test-admin")
    ok("审计列表取得到", status == 200 and audits.get("count", 0) >= 1, audits.get("count"))
    ok("审计列表带库内总数", audits.get("total", 0) >= audits.get("count", 0),
       (audits.get("total"), audits.get("count")))
    ok("审计类型带上条数", any(row["kind"] == "admin-denied" for row in
                              audits.get("kinds") or []),
       [row["kind"] for row in (audits.get("kinds") or [])[:5]])
    status, filtered = api_call("/api/admin/audits?kind=admin-denied", admin="test-admin")
    ok("按类型筛只剩这一类", status == 200 and filtered.get("count", 0) >= 1
       and all(row["kind"] == "admin-denied" for row in filtered.get("rows") or []),
       filtered.get("count"))
    status, searched = api_call("/api/admin/audits?q=BG7LZQ", admin="test-admin")
    ok("按关键词搜进 detail 里", status == 200 and searched.get("count", 0) >= 1,
       searched.get("count"))
    ok("搜出来的每一行都真的带这个字",
       all("BG7LZQ" in json.dumps(row, ensure_ascii=False).upper()
           for row in searched.get("rows") or []), searched.get("count"))
    status, page1 = api_call("/api/admin/audits?limit=2", admin="test-admin")
    cursor = page1.get("next_before")
    status, page2 = api_call("/api/admin/audits?limit=2&before=%s" % cursor,
                             admin="test-admin")
    ids1 = [row["id"] for row in page1.get("rows") or []]
    ids2 = [row["id"] for row in page2.get("rows") or []]
    ok("往后翻拿到更早的行", status == 200 and ids2 and max(ids2) < min(ids1),
       (ids1, ids2))
    ok("审计游标写错回 400", api_call("/api/admin/audits?before=abc",
                                  admin="test-admin")[0] == 400)

    # ⑪ 名单同步（MANAGER → SERVER）
    payload = {"mode": "incremental", "version": store.whitelist_version() + 1,
               "updated": now_iso(),
               "records": [{"call": "BG8XYZ", "status": "authorized", "version": 1}]}
    signed = dict(payload)
    signed["sig"] = auth.sign(payload, "manager-secret")
    status, body = api_call("/api/manager/whitelist", "POST", signed)
    ok("签名正确的名单被应用", status == 200 and body.get("ok"), status)
    ok("新名单条数带上", body.get("records") == 1, body)
    status, body = api_call("/api/manager/whitelist", "POST",
                            dict(payload, version=payload["version"] + 1, sig="deadbeef"))
    ok("签名不对被拒", status == 403, status)
    status, body = api_call("/api/manager/whitelist", "POST",
                            dict(payload, sig=auth.sign(payload, "manager-secret")))
    ok("版本没前进被拒", status == 409, status)

    # ⑫ 控制台：在线连接、报文检索、存档、踢连接
    status, body = api_call("/api/admin/clients", admin="test-admin")
    ok("在线连接列得出来", status == 200 and body.get("count", 0) >= 1, body.get("count"))
    rows = body.get("clients") or []
    ok("连接行带呼号与收发计数",
       any(row.get("call") == "BG7LZQ" and "received" in row and "sent" in row
           for row in rows), rows[:1])
    status, _ = api_call("/api/admin/clients", admin="wrong")
    ok("连接列表也要口令", status == 401, status)
    status, body = api_call("/api/admin/packets?q=test%20packet&limit=20", admin="test-admin")
    ok("内存环里查得到刚才的报文", status == 200 and body.get("count", 0) >= 1,
       body.get("count"))
    ok("检索结果带源呼号",
       all(row.get("src") == "BG7LZQ" for row in body.get("lines") or []),
       (body.get("lines") or [])[:1])
    status, body = api_call("/api/admin/packets?dir=out&limit=10", admin="test-admin")
    ok("只看发的时候全带 out",
       status == 200 and body.get("count", 0) >= 1
       and all(row["dir"] == "out" for row in body.get("lines") or []),
       body.get("count"))

    config.ARCHIVE_ENABLED = True
    a.sendall(b"BG7LZQ>APRS,TCPIP*:!3954.00N/11623.00E>archived line\r\n")
    time.sleep(0.5)
    status, body = api_call("/api/admin/archive?q=archived", admin="test-admin")
    ok("存档开着时整行进库", status == 200 and body.get("enabled")
       and body.get("count", 0) >= 1, body.get("count"))
    config.ARCHIVE_ENABLED = False
    a.sendall(b"BG7LZQ>APRS,TCPIP*:!3954.00N/11623.00E>not archived\r\n")
    time.sleep(0.5)
    status, body = api_call("/api/admin/archive?q=not%20archived", admin="test-admin")
    ok("存档关掉后不再进库", status == 200 and body.get("count") == 0,
       body.get("count"))

    status, body = api_call("/api/admin/kick", "POST", {"call": "BG6TA"},
                            admin="test-admin")
    ok("踢一个呼号返回踢掉的条数", status == 200 and body.get("kicked", 0) >= 1, body)
    observer.settimeout(2.0)
    closed = False
    while True:
        try:
            chunk = observer.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            closed = True
            break
    ok("被踢的连接从服务端断开", closed, "")
    status, body = api_call("/api/admin/kick", "POST", {"call": "N0NONE"},
                            admin="test-admin")
    ok("踢不存在的呼号返回 0", status == 200 and body.get("kicked") == 0, body)

    # ⑭ 草案 4.5：名单改了，已经建好的连接要断
    rev, reply = connect(node, "user BG6TA pass %d vers APRSLocus 2.1.11"
                         % aprslib.passcode("BG6TA"))
    ok("改名单前能正常连上", "logresp BG6TA verified" in reply, reply.strip())
    status, body = api_call("/api/admin/account", "POST",
                            {"call": "BG6TA", "status": "revoked"}, admin="test-admin")
    ok("改名单时把该断的连接断掉",
       status == 200 and "BG6TA" in (body.get("dropped") or []), body.get("dropped"))
    rev.settimeout(2.0)
    closed = False
    while True:
        try:
            chunk = rev.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            closed = True
            break
    ok("被撤销的连接确实断开", closed, "")
    rev.close()
    store.save_account("BG6TA", status="authorized")

    # ⑮ 草案 4.6：名单太久没同步就不再放行新连接
    old_sync = store.meta("whitelist_synced_at")
    config.WHITELIST_MAX_AGE_HOURS = 1
    store.set_meta("whitelist_synced_at", "2026-01-01T00:00:00+08:00")
    stale, reply = connect(node, "user BG7LZQ pass %d" % code, needle="not authorized")
    ok("名单过期没同步时拒新连接", "whitelist-stale" in reply, reply.strip())
    stale.close()
    config.WHITELIST_MAX_AGE_HOURS = 0
    store.set_meta("whitelist_synced_at", old_sync or now_iso())
    fresh, reply = connect(node, "user BG7LZQ pass %d" % code)
    ok("不限制时照常登录", "logresp BG7LZQ verified" in reply, reply.strip())
    fresh.close()

    # ⑯ 草案 7.3 / 7.4：管理拒绝留痕、启动自检、版本接口公布策略
    api_call("/api/admin/state", admin="wrong")
    status, state_body = api_call("/api/admin/state", admin="test-admin")
    kinds = [row["kind"] for row in state_body.get("audits") or []]
    ok("管理口令错也记一条审计", "admin-denied" in kinds, kinds[:6])
    ok("控制台带出拒绝计数", state_body["meta"].get("admin_denied", 0) >= 1,
       state_body["meta"].get("admin_denied"))
    report = self_check(store)
    ok("启动自检把库查一遍", report["db"] == "ok", report)
    status, health_body = api_call("/api/v1/health")
    ok("健康接口公布名单版本与同步时间",
       status == 200 and "whitelist_version" in health_body
       and "whitelist_synced_at" in health_body, list(health_body)[:6])
    status, version_body = api_call("/api/v1/version")
    ok("版本接口公布默认订阅策略",
       version_body.get("default_filter") == "all"
       and "r" in (version_body.get("filters") or []), version_body)

    # ⑰ 撤销后连不上
    store.save_account("BG8XYZ", status="revoked")
    sock, reply = connect(node, "user BG8XYZ pass %d" % aprslib.passcode("BG8XYZ"),
                          needle="not authorized")
    ok("撤销后的呼号被拒", "(revoked)" in reply, reply.strip())
    sock.close()

    # ⑱ 运行参数在控制台里直接改
    status, body = api_call("/api/admin/settings", admin="test-admin")
    keys = [row["key"] for row in body.get("rows") or []]
    ok("设置接口列出参数", status == 200 and len(keys) >= 20, status)
    ok("设置里有两块关键项", "ACN_ARCHIVE_ENABLED" in keys and "ACN_INGEST_FILTER" in keys,
       keys[:6])
    ok("设置带出 env 只读项", len(body.get("restart_only") or []) >= 4,
       len(body.get("restart_only") or []))
    status, body = api_call("/api/admin/settings", method="POST",
                            body={"values": {"ACN_RATE_LINES": "45"}}, admin="test-admin")
    ok("改一项立刻生效", status == 200 and config.RATE_LINES == 45, (status, config.RATE_LINES))
    ok("改过的值存进库", store.meta("setting.ACN_RATE_LINES") == "45",
       store.meta("setting.ACN_RATE_LINES"))
    status, body = api_call("/api/admin/settings", method="POST",
                            body={"values": {"ACN_RATE_LINES": "abc"}}, admin="test-admin")
    ok("坏值被挡下且报字段", status == 400 and "ACN_RATE_LINES" in (body.get("fields") or {}),
       (status, body.get("fields")))
    status, body = api_call("/api/admin/settings", method="POST",
                            body={"values": {"ACN_RATE_LINES": "999999999"}},
                            admin="test-admin")
    ok("越界值被挡下", status == 400, status)
    status, body = api_call("/api/admin/settings", method="POST",
                            body={"values": {"ACN_NO_SUCH_KEY": "1"}}, admin="test-admin")
    ok("不存在的项被拒", status == 400, status)
    status, body = api_call("/api/admin/settings", method="POST", body={}, admin="test-admin")
    ok("空改动被拒", status == 400, status)
    status, body = api_call("/api/admin/settings/reset", method="POST",
                            body={"keys": ["ACN_RATE_LINES"]}, admin="test-admin")
    ok("能退回 env 里的值", status == 200 and config.RATE_LINES == 60, config.RATE_LINES)
    status, body = api_call("/api/admin/settings", method="POST",
                            body={"values": {"ACN_ADMIN_PASSWORD": "new-pass"}},
                            admin="test-admin")
    ok("改管理口令", status == 200 and body.get("admin_password_changed") is True, status)
    status, _ = api_call("/api/admin/state", admin="new-pass")
    ok("新口令能用", status == 200, status)
    status, _ = api_call("/api/admin/state", admin="test-admin")
    ok("旧口令立即失效", status == 401, status)
    config.ADMIN_PASSWORD = "test-admin"

    # ⑲ 控制台的 css/js 从包里发，且不缓存
    for name, kind in (("/", "页面"), ("/assets/admin.js", "脚本"),
                       ("/assets/admin.css", "样式"), ("/assets/coast.json", "底图")):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (HTTP_PORT, name))
        with urllib.request.urlopen(request, timeout=6) as response:
            text = response.read().decode("utf-8", "replace")
        ok("控制台%s能取到" % kind, response.status == 200 and len(text) > 200,
           (response.status, len(text)))

    for item in (a, observer, read_only, far):
        item.close()
    node.stop()
    config.ARCHIVE_ENABLED = False


def test_ingest():
    """起一个假的 APRS-IS，验只读拉取：登录行、收到报文进 24 小时库、不转发。"""
    upstream = socket.socket()
    upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    upstream.bind(("127.0.0.1", UPSTREAM_PORT))
    upstream.listen(1)
    seen = {"login": ""}

    def serve():
        conn, _ = upstream.accept()
        conn.settimeout(10)
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buffer += chunk
        seen["login"] = buffer.decode("utf-8", "replace").strip()
        conn.sendall(b"# javAPRSSrvr 4.1.0b04\r\n")
        conn.sendall(b"# logresp BG7LZQ unverified, server T2TEST\r\n")
        conn.sendall(b"BG6TA>APRS,TCPIP*,qAR,T2TEST:!3954.00N/11623.00E>from upstream\r\n")
        # 一行坏字符（\x01）、一行能过字符集但不是 TNC2：两种丢法要分得开
        conn.sendall(b"BG6TB>APRS:\x01\r\n")
        conn.sendall(b"not a packet at all\r\n")
        time.sleep(3)
        conn.close()

    threading.Thread(target=serve, name="fake-is", daemon=True).start()
    store = Store(config.DB_PATH)
    feeder = Ingest(store)
    feeder.start()
    time.sleep(1.5)
    ok("拉取登录行用 pass -1 只收", "pass -1" in seen["login"], seen["login"])
    ok("拉取时带上了过滤器", "filter r/35/105/2000" in seen["login"])
    data = store.station("BG6TA")
    ok("上游的台站进了 24 小时库", data is not None, data and data.get("comment"))
    ok("上游台站记了来自网络", data is not None and data.get("via") == "rf",
       data and data.get("via"))
    stats = feeder.stats()
    ok("拉取统计里有条数", stats["stored"] >= 1, stats)
    ok("丢行总数 = 坏字符 + 解析不了",
       stats["dropped"] == stats["bad_chars"] + stats["unparsable"], stats)
    ok("坏字符与解析不了各计各的",
       stats["bad_chars"] >= 1 and stats["unparsable"] >= 1, stats)
    ok("有报文进来就不算哑", stats["stale"] is False, stats)
    feeder.stop()
    upstream.close()


def test_devices():
    """设备识别排行：目的呼号 → 设备名，一个台站只算最后一条。"""
    # 自己的库：别的用例也往同一个库里写台站，混在一起数不清。
    store = Store(os.path.join(DATA_DIR, "devices.db"))
    stamp = int(time.time())
    lines = [
        # BG6TA 先 APRS、后 APZ001：只应算在最后那条上
        "BG6TA>APRS:!3954.00N/11623.00E>a",
        "BG6TA>APZ001:!3954.10N/11623.10E>b",
        # BG6TB 也走 APZ001：与 BG6TA 并成一堆
        "BG6TB>APZ001:!3955.00N/11623.00E>c",
        # 认不出来的目的呼号：按原字样单独一堆
        "BG6TC>ZZZZZZ:!3956.00N/11623.00E>d",
    ]
    for offset, line in enumerate(lines):
        pkt = aprslib.parse(line)
        store.record_position(pkt, via="is", now=stamp + offset)
    rows, total = store.device_ranking(now=stamp + 10)
    by_call = {row["to_call"]: row for row in rows}
    ok("排行按目的呼号归堆", set(by_call) == {"APZ001", "ZZZZZZ"},
       [row["to_call"] for row in rows])
    ok("一个台站只算最后一条目的呼号", "APRS" not in by_call, by_call.get("APRS"))
    ok("同一目的呼号的两个台站合在一起",
       by_call["APZ001"]["stations"] == 2, by_call["APZ001"])
    ok("排行总数是窗口内的台站数", total == 3, total)
    ok("算出了台站数", store.station_count(now=stamp + 10) == 3)
    named = tocalls.model_of("APZ001")
    ok("目的呼号能查出设备名", named == "Experimental", named)
    ok("认不出的目的呼号返回空串", tocalls.model_of("ZZZZZZ") == "",
       tocalls.model_of("ZZZZZZ"))
    ok("带 ? 的键按位通配", tocalls.model_of("AP4RXX") != "",
       tocalls.model_of("AP4RXX"))
    ok("APRSLocus 自己的目的呼号认得出",
       tocalls.model_of("APALOC") == "APRSLocus",
       tocalls.model_of("APALOC"))
    ok("台站详情带着目的呼号",
       (store.station("BG6TC", now=stamp + 10) or {}).get("to_call") == "ZZZZZZ")


def main():
    test_passcode()
    test_parse()
    test_filters()
    test_end_to_end()
    test_ingest()
    test_devices()
    print("\n%d 项，过了 %d 项" % (len(PASSED) + len(FAILED), len(PASSED)))
    if FAILED:
        print("没过：" + "、".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
