"""SERVER：客户端接入、登录校验、报文转发、过滤器。

一个连接一个读线程一个写线程，转发走内存队列，不落盘（除 24 小时位置）。
"""

from __future__ import annotations

import hashlib
import re
import socket
import threading
import time
from collections import deque

from . import aprslib, auth, config, filters
from .store import now_iso

LOGIN_RE = re.compile(r"^user\s+(\S+)\s+pass\s+(-?\d+)(.*)$", re.I)
RF_ALIASES = ("WIDE", "RELAY", "TRACE", "GATE", "SGATE", "RFONLY", "NOGATE")


def via_of(pkt):
    """这条报文算走射频还是走网络。"""
    for entry in pkt.path:
        head = entry.split("*")[0]
        if head in ("qAR", "qAr"):
            return "rf"
    for entry in pkt.path:
        head = entry.split("*")[0].upper()
        if any(head.startswith(alias) for alias in RF_ALIASES):
            return "rf"
    return "is"


def with_q_construct(line, call):
    """按 APRS-IS 的做法补 `qAC,登录呼号`。"""
    head, sep, info = line.partition(":")
    if not sep:
        return line
    if ",qA" in head.upper():
        return line
    return "%s,qAC,%s:%s" % (head, call, info)


class LoginRequest:
    __slots__ = ("call", "code", "vers_name", "vers_ver", "account", "password",
                 "specs", "unknown")

    def __init__(self):
        self.call = ""
        self.code = 0
        self.vers_name = ""
        self.vers_ver = ""
        self.account = ""
        self.password = ""
        self.specs = []
        self.unknown = []

    @staticmethod
    def parse(text):
        """解析登录行；格式不对返回 None。"""
        match = LOGIN_RE.match(text.strip())
        if not match:
            return None
        request = LoginRequest()
        request.call = match.group(1).upper()
        try:
            request.code = int(match.group(2))
        except ValueError:
            return None
        rest = match.group(3) or ""
        tokens = rest.split()
        index = 0
        while index < len(tokens):
            word = tokens[index].lower()
            if word == "vers" and index + 2 < len(tokens):
                request.vers_name = tokens[index + 1]
                request.vers_ver = tokens[index + 2]
                index += 3
            elif word == "auth" and index + 2 < len(tokens):
                request.account = tokens[index + 1]
                request.password = tokens[index + 2]
                index += 3
            elif word == "filter":
                request.specs = tokens[index + 1:]
                index = len(tokens)
            else:
                request.unknown.append(tokens[index])
                index += 1
        return request


class Client:
    """一条客户端连接。"""

    def __init__(self, hub, sock, address):
        self.hub = hub
        self.sock = sock
        self.address = address
        self.call = ""
        self.verified = False
        self.read_only = False
        self.ctx = filters.Context()
        self.filters = filters.Filter()
        self.queue = deque()
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.alive = True
        self.opened = time.time()
        self.sent = 0
        self.received = 0
        self.dropped = 0
        self.rate = deque()

    # ── 发送 ──
    def send(self, line):
        if not self.alive:
            return
        with self.lock:
            if len(self.queue) >= config.QUEUE_LIMIT:
                self.queue.popleft()                       # 队列满了丢最旧的
                self.dropped += 1
            self.queue.append(line)
        self.event.set()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        self.event.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def writer(self):
        while self.alive:
            self.event.wait(1.0)
            self.event.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        break
                    line = self.queue.popleft()
                try:
                    self.sock.sendall((line + "\r\n").encode("utf-8", "replace"))
                    self.sent += 1
                except OSError:
                    self.alive = False
                    return

    # ── 限速 ──
    def rate_ok(self):
        now = time.time()
        self.rate.append(now)
        while self.rate and now - self.rate[0] > config.RATE_WINDOW:
            self.rate.popleft()
        return len(self.rate) <= config.RATE_LINES

    def match(self, pkt):
        return self.filters.match(pkt, self.ctx)

    def snapshot(self):
        return {
            "call": self.call,
            "address": "%s:%s" % self.address[:2],
            "verified": self.verified,
            "read_only": self.read_only,
            "vers": self.vers,
            "filters": self.filters.active,
            "since": int(self.opened),
            "sent": self.sent,
            "received": self.received,
            "dropped": self.dropped,
        }

    vers = ""


class Hub:
    """连接的集合与转发。"""

    def __init__(self, store):
        self.store = store
        self.clients = set()
        self.lock = threading.Lock()
        self.server_sockets = []
        self.threads = []
        self.running = False
        self.started_at = int(time.time())
        self.packets_in = 0
        self.packets_out = 0
        self.logins_ok = 0
        self.logins_failed = 0
        self.seen = {}
        self._seen_lock = threading.Lock()

    # ── 启停 ──
    def start(self):
        if self.running:
            return
        self.running = True
        self._listen(config.TCP_HOST, config.TCP_PORT)
        if config.TCP_PORT_TLS and config.TLS_CERT and config.TLS_KEY:
            self._listen(config.TCP_HOST, config.TCP_PORT_TLS, tls=True)
        self._thread(self._sweep, "sweep")

    def _listen(self, host, port, tls=False):
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                             socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        if tls:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(config.TLS_CERT, config.TLS_KEY)
            sock = context.wrap_socket(sock, server_side=True)
        self.server_sockets.append(sock)
        self._thread(lambda: self._accept(sock), "accept-%d" % port)

    def _thread(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def stop(self):
        self.running = False
        for sock in self.server_sockets:
            try:
                sock.close()
            except OSError:
                pass
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.close()

    def _accept(self, sock):
        while self.running:
            try:
                conn, address = sock.accept()
            except OSError:
                return
            if len(self.clients) >= config.MAX_CONN:
                try:
                    conn.sendall(b"# aprsnetcn: server busy\r\n")
                    conn.close()
                except OSError:
                    pass
                continue
            client = Client(self, conn, address)
            self._thread(lambda c=client: self._serve(c), "client")

    # ── 一条连接的生命周期 ──
    def _serve(self, client):
        conn = client.sock
        conn.settimeout(config.LOGIN_TIMEOUT)
        buffer = b""
        threading.Thread(target=client.writer, name="writer", daemon=True).start()
        client.send("# aprsnetcn %s %s" % (config.SERVER_ID, config.PROTO_VERSION))
        try:
            line, buffer = self._readline(conn, buffer)
        except (OSError, socket.timeout):
            client.close()
            return
        if not self._login(client, line):
            return
        client.sock.settimeout(None)
        with self.lock:
            self.clients.add(client)
        client.send("# aprsnetcn: logged in as %s" % client.call)
        try:
            while client.alive and self.running:
                line, buffer = self._readline(conn, buffer)
                if line == "":
                    break
                self._on_line(client, line)
        except (OSError, socket.timeout):
            pass
        finally:
            with self.lock:
                self.clients.discard(client)
            client.close()
            self.store.audit("disconnect", {"call": client.call})

    @staticmethod
    def _readline(conn, buffer):
        while b"\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return "", buffer
            buffer += chunk
            if len(buffer) > config.MAX_LINE * 4:
                return "", buffer
        raw, _, rest = buffer.partition(b"\n")
        text = raw.decode("utf-8", "replace").rstrip("\r")
        return text, rest

    def _login(self, client, line):
        login = LoginRequest.parse(line or "")
        if login is None:
            self._reject(client, "bad-login-format")
            return False
        client.vers = ("%s %s" % (login.vers_name, login.vers_ver)).strip()
        if not aprslib.valid_call(login.call):
            self._reject(client, "bad-call")
            return False
        # 3. Passcode
        expected = aprslib.passcode(login.call)
        read_only = login.code == -1
        verified = login.code == expected
        if config.PASSCODE_MODE == "strict" and not verified:
            self._reject(client, "passcode")
            return False
        # 4. 白名单（按主呼号）
        root = login.call.split("-")[0]
        record = self.store.account(root) or self.store.account(login.call)
        allowed, reason = auth.may_login(record, auth_now())
        if not allowed:
            self._reject(client, reason)
            return False
        # 5. 账号与密码
        if record.get("pass_hash"):
            if not login.account or not login.password:
                self._reject(client, "auth-required")
                return False
            by_name = self.store.account_by_name(login.account)
            if (by_name is None or by_name["call"] != record["call"]
                    or not auth.verify_password(login.password, record["pass_hash"])):
                self._reject(client, "auth-failed")
                return False
            client.verified = True
        else:
            client.verified = verified
        client.read_only = read_only or not client.verified
        client.call = login.call
        client.ctx.call = login.call
        if config.PASSCODE_MODE != "off" and not verified:
            client.read_only = True
        # 6. 过滤器
        try:
            client.filters = filters.Filter.parse(login.specs)
        except filters.FilterError as error:
            client.send("# aprsnetcn: filter rejected (%s)" % error)
        self.logins_ok += 1
        client.send("# logresp %s %s, server %s"
                    % (login.call, "verified" if client.verified else "unverified",
                       config.SERVER_ID))
        for spec in client.filters.active:
            client.send("# filter %s active" % spec)
        self.store.audit("login", {"call": login.call, "verified": client.verified,
                                   "read_only": client.read_only,
                                   "vers": client.vers, "address": client.address[0]})
        return True

    def _reject(self, client, reason):
        self.logins_failed += 1
        try:
            client.sock.sendall(("# logresp %s unverified, server %s\r\n"
                                 % (client.call or "N0CALL", config.SERVER_ID)).encode())
            client.sock.sendall(("# aprsnetcn: not authorized (%s)\r\n"
                                 % reason).encode())
        except OSError:
            pass
        self.store.audit("login-rejected", {"reason": reason,
                                            "address": client.address[0]})
        client.close()

    # ── 报文 ──
    def _on_line(self, client, line):
        if not line:
            return
        if line.startswith("#"):
            if line.lower().startswith("filter"):
                specs = line.split()[1:]
                try:
                    client.filters = filters.Filter.parse(specs)
                    for spec in client.filters.active:
                        client.send("# filter %s active" % spec)
                except filters.FilterError as error:
                    client.send("# aprsnetcn: filter rejected (%s)" % error)
            return
        if not client.rate_ok():
            client.send("# aprsnetcn: rate limited")
            return
        text, error = aprslib.clean_line(line, config.MAX_LINE)
        if text is None:
            client.send("# aprsnetcn: bad packet (%s)" % error)
            return
        pkt = aprslib.parse(text)
        if pkt is None:
            client.send("# aprsnetcn: bad packet (unparsable)")
            self.store.audit("packet-rejected", {"call": client.call,
                                                 "line": text[:120]})
            return
        if client.read_only:
            client.send("# aprsnetcn: read-only, packet dropped")
            return
        if pkt.src.split("-")[0] != client.call.split("-")[0]:
            if config.SRC_POLICY == "reject":
                client.send("# aprsnetcn: source not allowed")
                return
            if config.SRC_POLICY == "warn":
                self.store.audit("source-mismatch", {"login": client.call,
                                                     "source": pkt.src})
        if pkt.lat is not None and pkt.src == client.call:
            client.ctx.lat, client.ctx.lon = pkt.lat, pkt.lon
        client.received += 1
        self.packets_in += 1
        via = via_of(pkt)
        self.store.record_position(pkt, via=via, src_call=client.call)
        out = with_q_construct(text, client.call) if config.ADD_Q_CONSTRUCT else text
        self.broadcast(out, pkt, origin=client)

    def broadcast(self, line, pkt, origin=None):
        digest = hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).digest()
        now = time.time()
        with self._seen_lock:
            if digest in self.seen and now - self.seen[digest] < 30:
                return
            self.seen[digest] = now
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            if client is origin or not client.alive:
                continue
            try:
                if client.match(pkt):
                    client.send(line)
                    self.packets_out += 1
            except Exception:                              # noqa: BLE001
                continue

    def _sweep(self):
        while self.running:
            time.sleep(30)
            now = time.time()
            with self._seen_lock:
                for digest, stamp in list(self.seen.items()):
                    if now - stamp > 30:
                        self.seen.pop(digest, None)

    # ── 状态 ──
    def stats(self):
        with self.lock:
            clients = [client.snapshot() for client in self.clients]
        return {
            "running": self.running,
            "uptime": int(time.time()) - self.started_at,
            "clients": clients,
            "connections": len(clients),
            "packets_in": self.packets_in,
            "packets_out": self.packets_out,
            "logins_ok": self.logins_ok,
            "logins_failed": self.logins_failed,
            "ports": [config.TCP_PORT] + ([config.TCP_PORT_TLS] if config.TCP_PORT_TLS else []),
        }


def auth_now():
    return now_iso()
