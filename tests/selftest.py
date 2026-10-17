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
    "ACN_RETENTION_HOURS": "24",
    "ACN_INGEST_ENABLED": "1",
    "ACN_INGEST_HOST": "127.0.0.1",
    "ACN_INGEST_PORT": str(UPSTREAM_PORT),
    "ACN_INGEST_LOGIN": "BG7LZQ",
    "ACN_INGEST_FILTER": "r/35/105/2000",
})

from aprsnetcn import api, aprslib, auth, config, filters  # noqa: E402
from aprsnetcn.hub import Hub  # noqa: E402
from aprsnetcn.ingest import Ingest  # noqa: E402
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
    # ② 没带账号密码
    b, reply = connect(node, "user BG7LZQ pass %d" % code, needle="not authorized")
    ok("设了口令的账号必须带 auth", "auth-required" in reply, reply.strip())
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

    # ⑩ 管理接口
    status, state_body = api_call("/api/admin/state", admin="test-admin")
    ok("控制台状态要口令", status == 200 and state_body.get("ok"), status)
    status, _ = api_call("/api/admin/state", admin="wrong")
    ok("错口令被拒", status == 401, status)
    status, saved = api_call("/api/admin/account", "POST",
                             {"call": "bh4hga", "status": "authorized"}, admin="test-admin")
    ok("控制台加账号", status == 200 and saved["account"]["call"] == "BH4HGA", status)
    ok("加账号时名单版本前进", saved.get("version", 0) >= 1, saved.get("version"))

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

    # ⑫ 撤销后连不上
    store.save_account("BG8XYZ", status="revoked")
    sock, reply = connect(node, "user BG8XYZ pass %d" % aprslib.passcode("BG8XYZ"),
                          needle="not authorized")
    ok("撤销后的呼号被拒", "(revoked)" in reply, reply.strip())
    sock.close()

    for item in (a, observer, read_only, far):
        item.close()
    node.stop()


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
    feeder.stop()
    upstream.close()


def main():
    test_passcode()
    test_parse()
    test_filters()
    test_end_to_end()
    test_ingest()
    print("\n%d 项，过了 %d 项" % (len(PASSED) + len(FAILED), len(PASSED)))
    if FAILED:
        print("没过：" + "、".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
