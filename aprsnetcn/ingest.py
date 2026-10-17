"""只读地拉一份 APRS-IS 的报文，补进 24 小时台站。

草案 8.2：引入外部数据要明确来源、过滤与转发规则。所以这里默认关，
打开时必须给出上游地址与过滤器；报文默认只进 24 小时库，不转发给客户端。
"""

from __future__ import annotations

import socket
import threading
import time

from . import aprslib, config


class Ingest:
    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub
        self.running = False
        self.thread = None
        self.sock = None
        self.connected = False
        self.lines = 0
        self.stored = 0
        self.dropped = 0
        self.opened_at = None
        self.last_at = None
        self.error = ""

    def start(self):
        if not config.INGEST_ENABLED or self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, name="ingest", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        backoff = 5
        while self.running:
            try:
                self._session()
                backoff = 5
            except Exception as error:                     # noqa: BLE001
                self.error = "%s: %s" % (type(error).__name__, error)
                self.store.audit("ingest-error", {"error": self.error})
            finally:
                self.connected = False
                self.sock = None
            if not self.running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _session(self):
        sock = socket.create_connection((config.INGEST_HOST, config.INGEST_PORT),
                                        timeout=20)
        self.sock = sock
        sock.settimeout(180)
        self.connected = True
        self.opened_at = int(time.time())
        self.error = ""
        login = ("user %s pass -1 vers %s %s filter %s"
                 % (config.INGEST_LOGIN, config.SERVER_ID, config.PROTO_VERSION,
                    config.INGEST_FILTER))
        sock.sendall((login + "\r\n").encode())
        self.store.audit("ingest-connect", {"host": config.INGEST_HOST,
                                            "port": config.INGEST_PORT,
                                            "filter": config.INGEST_FILTER})
        buffer = b""
        while self.running:
            chunk = sock.recv(8192)
            if not chunk:
                raise ConnectionError("upstream closed")
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                self._line(raw.decode("utf-8", "replace").rstrip("\r"))
        try:
            sock.close()
        except OSError:
            pass

    def _line(self, line):
        if not line or line.startswith("#"):
            return
        self.lines += 1
        self.last_at = int(time.time())
        text, error = aprslib.clean_line(line, config.MAX_LINE)
        if text is None:
            self.dropped += 1
            return
        pkt = aprslib.parse(text)
        if pkt is None:
            self.dropped += 1
            return
        if self.store.record_position(pkt, via=aprslib.via_of(pkt),
                                      src_call=config.INGEST_LOGIN):
            self.stored += 1
        if config.INGEST_FORWARD and self.hub is not None:
            self.hub.broadcast(text, pkt)

    def stats(self):
        return {
            "enabled": config.INGEST_ENABLED,
            "connected": self.connected,
            "host": "%s:%d" % (config.INGEST_HOST, config.INGEST_PORT),
            "filter": config.INGEST_FILTER,
            "lines": self.lines,
            "stored": self.stored,
            "dropped": self.dropped,
            "opened_at": self.opened_at,
            "last_at": self.last_at,
            "error": self.error,
            "forward": config.INGEST_FORWARD,
        }
