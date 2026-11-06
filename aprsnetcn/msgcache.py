"""按呼号缓存消息：服务器自己看到的报文也进这个人的消息里。

APRSLocus 的消息同步原本只有「客户端把自己手里的报上来」。手机没开机、
消息落在空中的那段，谁也没记下。这里在转发与拉取的当口顺手记一份：
收件人是名单里的呼号就记一条「收到的」，发件人是名单里的呼号就记一条「发出的」，
对方回的 ACK 再把自己发的那一条标成已确认。客户端下次同步就把它们拿回去了。

只用报文自己带的东西，不猜：正文取 `:收件人:正文` 里那一截，编号取 `{编号`。
同一句话在两台设备、两条链路上各看到一遍时，靠 (呼号, 方向, 对象, 正文, 时间)
判成同一条，只留一行。
"""

from __future__ import annotations

import threading
import time
import zlib

from . import aprslib, config


def root_of(call):
    """去掉 SSID 的根呼号：名单里写的是根呼号，报文里带的是整串。"""
    return (call or "").strip().upper().split("-")[0]


def mid_of(ts_ms, line):
    """服务器这一侧的 mid：时间戳 + 整行报文的校验。同一行重放会得到同一个。"""
    digest = zlib.crc32((line or "").encode("utf-8", "replace")) & 0xFFFFFFFF
    return "aprs-%d-%08x" % (int(ts_ms), digest)


class MsgCache:
    """名单里那些呼号的消息缓存。Hub 与 Ingest 各调 observe()。"""

    def __init__(self, store, ttl=60):
        self.store = store
        self.ttl = ttl
        self._calls = frozenset()
        self._at = 0.0
        self._lock = threading.Lock()
        self.kept = 0               # 累计写进去几行，控制台要看

    def calls(self, now=None):
        """名单里的根呼号。一分钟查一次库，别每条报文都翻一遍名单。"""
        now = time.time() if now is None else now
        with self._lock:
            if self._at and now - self._at < self.ttl:
                return self._calls
            fresh = frozenset(
                root_of(row.get("call")) for row in self.store.accounts()
                if root_of(row.get("call")))
            self._calls = fresh
            self._at = now
        return self._calls

    def observe(self, pkt, ts=None):
        """一条报文进来：是消息就按收件人与发件人各记一次。返回写了几行。"""
        if not config.MSG_CACHE or pkt is None:
            return 0
        msg = aprslib.message_of(pkt)
        if msg is None:
            return 0
        calls = self.calls()
        if not calls:
            return 0
        ts_ms = int(round((time.time() if ts is None else ts) * 1000))
        to_root = root_of(msg["to"])
        src_root = root_of(pkt.src)
        if msg["kind"] in ("ack", "rej"):
            # 回执：被确认的是收件人发出去的那一条
            if to_root in calls and msg["pid"]:
                if self.store.cache_ack(to_root, pkt.src, msg["pid"]):
                    self.kept += 1
                    return 1
            return 0
        text = msg["text"]
        if not text:
            return 0
        mid = mid_of(ts_ms, pkt.raw)
        kept = 0
        if to_root in calls:
            if self.store.cache_message(to_root, mid, ts_ms, pkt.src, "in", text,
                                        msg["pid"], limit=config.MSG_CACHE_LIMIT,
                                        window=config.MSG_CACHE_DEDUPE):
                kept += 1
        if src_root in calls and src_root != to_root:
            if self.store.cache_message(src_root, mid + "-out", ts_ms, msg["to"],
                                        "out", text, msg["pid"],
                                        limit=config.MSG_CACHE_LIMIT,
                                        window=config.MSG_CACHE_DEDUPE):
                kept += 1
        self.kept += kept
        return kept
