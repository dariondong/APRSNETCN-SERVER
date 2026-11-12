"""HTTP：公开的 24 小时台站 API + MANAGER 管理接口 + 一个控制台页面。"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (__version__, aprslib, auth, config, filters,
               hub as hub_module, settings as settings_module,
               store as store_module, tocalls as tocalls_module)

BEIJING = store_module.BEIJING


class Api:
    """状态容器：store、hub、ingest、token、限流。"""

    def __init__(self, store, hub, ingest=None):
        self.store = store
        self.hub = hub
        self.ingest = ingest
        self.tokens = {}
        self.lock = threading.Lock()
        self.rate = {}
        self.public_rate = {}
        self.register_rate = {}
        self.admin_denied = 0

    def enforce_whitelist(self, reason="whitelist-changed"):
        """草案 4.5：名单变了就把已经建好、现在不该放行的连接断掉。"""
        now = store_module.now_iso()
        with self.hub.lock:
            live = sorted({client.call for client in self.hub.clients if client.call})
        dropped = []
        for call in live:
            record = self.store.account(call.split("-")[0]) or self.store.account(call)
            allowed, _why = auth.may_login(record, now)
            if not allowed:
                dropped.append(call)
        for call in dropped:
            self.hub.kick(call)
        if dropped:
            self.store.audit("revoked-disconnect", {"calls": dropped, "reason": reason})
        return dropped

    # ── token ──
    def issue(self, call):
        token = auth.new_token()
        expires = time.time() + config.TOKEN_TTL
        with self.lock:
            self.tokens[token] = {"call": call, "expires": expires}
        return token, expires

    def owner(self, token):
        with self.lock:
            item = self.tokens.get(token)
            if not item:
                return None
            if item["expires"] < time.time():
                self.tokens.pop(token, None)
                return None
            return item["call"]

    def rate_ok(self, token):
        now = time.time()
        with self.lock:
            window = self.rate.setdefault(token, [])
            window.append(now)
            while window and now - window[0] > 60:
                window.pop(0)
            return len(window) <= config.API_RATE_MINUTE

    def public_ok(self, address):
        """公开接口按来源 IP 限流。

        不验 token，所以只能按 IP 数：限的是「同一个浏览器一秒刷几十下」，
        不是防人。表格长到一定规模就整表清一次，否则一个扫描器能把内存撑开。
        """
        now = time.time()
        with self.lock:
            if len(self.public_rate) > 2000:
                self.public_rate.clear()
            window = self.public_rate.setdefault(address or "-", [])
            window.append(now)
            while window and now - window[0] > 60:
                window.pop(0)
            return len(window) <= config.PUBLIC_RATE_MINUTE

    def register_ok(self, address):
        """自助注册按来源 IP 限流：一小时内同一个 IP 最多那几次。

        失败的那几次也算：不然猜呼号、试口令、刷账号都绕得过。
        """
        now = time.time()
        with self.lock:
            if len(self.register_rate) > 2000:
                self.register_rate.clear()
            window = self.register_rate.setdefault(address or "-", [])
            window.append(now)
            while window and now - window[0] > 3600:
                window.pop(0)
            return len(window) <= config.REGISTER_IP_HOUR

    # ── 个人数据同步的令牌 ──
    def sync_issue(self, call, device):
        """口令换一个长期令牌；库只存哈希。"""
        token = auth.new_token()
        expires = self.store.sync_put_token(auth.token_hash(token), call, device,
                                            config.SYNC_TOKEN_TTL)
        return token, expires

    def sync_owner(self, token):
        if not token:
            return None
        return self.store.sync_token(auth.token_hash(token))


def _day_ok(day):
    """轨迹档名只能是真实存在的日期（`2026-13-99` 这种不算）。"""
    try:
        datetime.strptime((day or "").strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def _bbox(value):
    if not value:
        return None
    parts = [piece for piece in re.split(r"[,\s]+", value.strip()) if piece]
    if len(parts) != 4:
        raise ValueError("bbox")
    try:
        numbers = [float(piece) for piece in parts]
    except ValueError:
        raise ValueError("bbox")
    return numbers


def _list_params(query, default_limit=500):
    """列表接口的公共参数：window / since / bbox / limit / offset。

    带 token 的 `/api/v1/stations` 与不带 token 的 `/api/public/stations` 是同一份
    语义，写两份迟早对不上，所以只留这一处。
    """
    window = (query.get("window") or ["24h"])[0]
    hours = 24
    match = re.match(r"^(\d{1,3})h?$", window)
    if match:
        hours = max(1, min(config.RETENTION_HOURS, int(match.group(1))))
    since = (query.get("since") or [""])[0]
    try:
        since_epoch = (int(datetime.fromisoformat(since.replace("Z", "+00:00"))
                           .timestamp()) if since else None)
    except ValueError:
        return None, "bad-since"
    try:
        bbox = _bbox((query.get("bbox") or [""])[0])
    except ValueError:
        return None, "bad-bbox"
    try:
        limit = max(1, min(5000, int((query.get("limit") or [str(default_limit)])[0])))
        offset = max(0, int((query.get("offset") or ["0"])[0]))
    except ValueError:
        return None, "bad-limit"
    return {"hours": hours, "since": since_epoch, "bbox": bbox,
            "limit": limit, "offset": offset}, ""


def make_server(store, hub, ingest=None):
    api = Api(store, hub, ingest)
    handler = _handler_class(api)
    server = ThreadingHTTPServer((config.HTTP_HOST, config.HTTP_PORT), handler)
    server.daemon_threads = True
    return server, api


def _handler_class(api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "APRSNETCN/" + __version__
        protocol_version = "HTTP/1.1"

        # ── 基础 ──
        def log_message(self, fmt, *args):                 # 静音，自己记审计
            if api.store and self.path.startswith("/api/admin"):
                return
            return

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, X-ACN-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status, text):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 控制台的 HTML 也不缓存：新加的一块不是在浏览器里硬刷新才看得到。
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            raw = self._raw()
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _raw(self, limit=None):
            """请求体原始字节。支持 gzip 压缩（备份与轨迹比较大）。

            上限默认 8 MB：一天的轨迹与一份备份都能整份进来；再大就当没收。
            """
            length = int(self.headers.get("Content-Length") or 0)
            cap = int(limit or config.HTTP_MAX_BODY)
            if length <= 0 or length > cap:
                return None
            raw = self.rfile.read(length)
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    return None
                if len(raw) > cap * 4:
                    return None
            return raw

        def _bearer(self):
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return ""

        def _admin_ok(self):
            if not config.ADMIN_PASSWORD:
                return False
            if auth.constant_equal(self.headers.get("X-ACN-Token"), config.ADMIN_PASSWORD):
                return True
            # 草案 7.3：管理操作被拒也该留痕
            api.admin_denied += 1
            api.store.audit("admin-denied", {"path": urlparse(self.path).path,
                                             "address": self.client_address[0]})
            return False

        # ── 路由 ──
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path == "/":
                    return self._index()
                if path == "/api/v1/health":
                    return self._health()
                if path == "/api/v1/version":
                    return self._version()
                if path == "/api/v1/register":
                    return self._register_info()
                if path == "/api/v1/openapi.json":
                    return self._openapi()
                if path == "/stations":
                    return self._stations_page()
                if path == "/register":
                    return self._register_page()
                if path == "/assets/coast.json":
                    return self._asset("coast.json", "application/json; charset=utf-8")
                if path == "/assets/admin.css":
                    return self._console_asset("admin.css", "text/css; charset=utf-8")
                if path == "/assets/admin.js":
                    return self._console_asset("admin.js", "application/javascript; charset=utf-8")
                if path == "/api/public/stations":
                    return self._public_stations(query)
                if path.startswith("/api/public/stations/"):
                    return self._public_station(path.rsplit("/", 1)[-1])
                if path == "/api/v1/stations":
                    return self._stations(query)
                if path.startswith("/api/v1/stations/"):
                    return self._station(path.rsplit("/", 1)[-1])
                if path == "/api/v1/sync/state":
                    return self._sync_state()
                if path == "/api/v1/sync/messages":
                    return self._sync_messages_list(query)
                if path == "/api/v1/sync/tracks":
                    return self._sync_track_days()
                if path.startswith("/api/v1/sync/tracks/"):
                    return self._sync_track(path.rsplit("/", 1)[-1])
                if path == "/api/v1/sync/backup":
                    return self._sync_backup_list()
                if path.startswith("/api/v1/sync/backup/"):
                    return self._sync_backup(path.rsplit("/", 1)[-1])
                if path == "/api/admin/sync":
                    return self._admin_sync()
                if path == "/api/admin/state":
                    return self._admin_state()
                if path == "/api/admin/clients":
                    return self._admin_clients()
                if path == "/api/admin/packets":
                    return self._admin_packets(query)
                if path == "/api/admin/stations":
                    return self._admin_stations(query)
                if path == "/api/admin/devices":
                    return self._admin_devices(query)
                if path.startswith("/api/admin/stations/"):
                    return self._admin_station(path.rsplit("/", 1)[-1])
                if path == "/api/admin/archive":
                    return self._admin_archive(query)
                if path == "/api/admin/audits":
                    return self._admin_audits(query)
                if path == "/api/admin/whitelist":
                    return self._admin_whitelist()
                if path == "/api/admin/settings":
                    return self._admin_settings()
                if path == "/api/manager/whitelist/status":
                    return self._manager_status()
                return self._json(404, {"error": "not-found"})
            except Exception as error:                     # noqa: BLE001
                return self._json(500, {"error": "server-error", "message": str(error)})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/api/v1/login":
                    return self._login()
                if path == "/api/v1/register":
                    return self._register()
                if path == "/api/v1/sync/login":
                    return self._sync_login()
                if path == "/api/v1/sync/logout":
                    return self._sync_logout()
                if path == "/api/v1/sync/messages":
                    return self._sync_messages_put()
                if path == "/api/admin/account":
                    return self._admin_account()
                if path == "/api/admin/account/delete":
                    return self._admin_delete()
                if path == "/api/admin/publish":
                    return self._admin_publish()
                if path == "/api/admin/kick":
                    return self._admin_kick()
                if path == "/api/admin/settings":
                    return self._admin_settings_save()
                if path == "/api/admin/settings/reset":
                    return self._admin_settings_reset()
                if path == "/api/manager/whitelist":
                    return self._manager_publish()
                return self._json(404, {"error": "not-found"})
            except Exception as error:                     # noqa: BLE001
                return self._json(500, {"error": "server-error", "message": str(error)})

        def do_PUT(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path.startswith("/api/v1/sync/tracks/"):
                    return self._sync_track_put(path.rsplit("/", 1)[-1])
                if path == "/api/v1/sync/backup":
                    return self._sync_backup_put(query)
                return self._json(404, {"error": "not-found"})
            except Exception as error:                     # noqa: BLE001
                return self._json(500, {"error": "server-error", "message": str(error)})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/api/v1/sync":
                    return self._sync_clear()
                if path.startswith("/api/v1/sync/tracks/"):
                    return self._sync_track_delete(path.rsplit("/", 1)[-1])
                if path.startswith("/api/v1/sync/backup/"):
                    return self._sync_backup_delete(path.rsplit("/", 1)[-1])
                if path.startswith("/api/v1/sync/devices/"):
                    return self._sync_device_delete(path.rsplit("/", 1)[-1])
                return self._json(404, {"error": "not-found"})
            except Exception as error:                     # noqa: BLE001
                return self._json(500, {"error": "server-error", "message": str(error)})

        # ── 页面 ──
        def _index(self):
            path = os.path.join(os.path.dirname(__file__), "admin.html")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return self._html(200, handle.read())
            except OSError:
                return self._json(200, {"ok": True, "server": config.SERVER_ID,
                                        "proto": __version__})

        def _stations_page(self):
            """24 小时台站页：地图 + 点选详情。默认不对外，开了 PUBLIC_API 才服务。"""
            if not config.PUBLIC_API:
                return self._json(403, {"error": "not-public"})
            path = os.path.join(os.path.dirname(__file__), "stations.html")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return self._html(200, handle.read())
            except OSError:
                return self._json(404, {"error": "page-missing"})

        def _register_page(self):
            """自助注册页。注册关着也照发，页面自己会说没开。"""
            return self._console_asset("register.html", "text/html; charset=utf-8")

        def _asset(self, name, content_type):
            path = os.path.join(os.path.dirname(__file__), name)
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                return self._json(404, {"error": "asset-missing"})
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _console_asset(self, name, content_type):
            """控制台自己的 css/js：每次都不缓存，改了部署就生效，不用改版本号。"""
            path = os.path.join(os.path.dirname(__file__), name)
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                return self._json(404, {"error": "asset-missing"})
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ── 公开 API ──
        def _health(self):
            stats = api.hub.stats()
            self._json(200, {
                "ok": True,
                "server": config.SERVER_ID,
                "proto": __version__,
                "uptime": stats["uptime"],
                "clients": stats["connections"],
                "packets_in": stats["packets_in"],
                "packets_out": stats["packets_out"],
                "stations_24h": api.store.station_count(),
                "positions_24h": api.store.position_count(),
                "whitelist_version": api.store.whitelist_version(),
                "whitelist_synced_at": api.store.meta("whitelist_synced_at"),
                "ingest": api.ingest.stats() if api.ingest else {"enabled": False},
                "time": store_module.now_iso(),
            })

        def _version(self):
            self._json(200, {
                "ok": True,
                "proto": __version__,
                "server": config.SERVER_ID,
                "retention_hours": config.RETENTION_HOURS,
                "passcode_mode": config.PASSCODE_MODE,
                "src_policy": config.SRC_POLICY,
                "auth_mode": config.AUTH_MODE,
                "whitelist_max_age_hours": config.WHITELIST_MAX_AGE_HOURS,
                "default_filter": "all",
                "login_extensions": ["vers", "auth", "filter"],
                "public_api": config.PUBLIC_API,
                "register": config.REGISTER_ENABLED,
                "filters": list(filters.KNOWN),
            })

        # ── 自助注册 ──
        # 不限人数的那台靠这两条开账号：注册即授权，呼号就是名单里的那条。
        def _register_info(self):
            """开不开注册、已经收了多少、口令最少几位。"""
            count = api.store.account_count()
            self._json(200, {
                "ok": True,
                "open": config.REGISTER_ENABLED,
                "full": bool(config.REGISTER_MAX and count >= config.REGISTER_MAX),
                "accounts": count,
                "max": config.REGISTER_MAX,
                "password_min": config.REGISTER_PW_MIN,
                "server": config.SERVER_ID,
                "proto": __version__,
                "tcp_port": config.TCP_PORT,
                "auth_mode": config.AUTH_MODE,
            })

        def _register(self):
            if not config.REGISTER_ENABLED:
                return self._json(403, {"error": "register-closed"})
            address = self.client_address[0]
            if not api.register_ok(address):
                api.store.audit("register-limited", {"address": address})
                return self._json(429, {"error": "rate-limited",
                                        "per_hour": config.REGISTER_IP_HOUR})
            body = self._body()
            root = (body.get("call") or "").split("-")[0].upper().strip()
            if not aprslib.valid_call(root):
                return self._json(400, {"error": "bad-call"})
            password = body.get("password")
            if not isinstance(password, str) or len(password) < config.REGISTER_PW_MIN:
                return self._json(400, {"error": "weak-password",
                                        "min": config.REGISTER_PW_MIN})
            if len(password) > 128:
                return self._json(400, {"error": "long-password", "max": 128})
            account = (body.get("account") or root).strip().lower()
            if not re.match(r"^[a-z0-9][a-z0-9_.-]{2,31}$", account):
                return self._json(400, {"error": "bad-account"})
            if config.REGISTER_MAX and api.store.account_count() >= config.REGISTER_MAX:
                return self._json(403, {"error": "register-full",
                                        "max": config.REGISTER_MAX})
            record, error = api.store.create_account(
                root, account=account,
                pass_hash=auth.hash_password(password),
                status="authorized",
                note="self-register %s" % address)
            if not record:
                api.store.audit("register-rejected", {"call": root, "account": account,
                                                      "reason": error,
                                                      "address": address})
                return self._json(409, {"error": error or "taken"})
            version = api.store.bump_whitelist_version()
            api.store.set_meta("whitelist_synced_at", store_module.now_iso())
            api.store.audit("register", {"call": root, "account": account,
                                          "address": address, "version": version})
            self._json(201, {
                "ok": True,
                "call": root,
                "account": account,
                "passcode": aprslib.passcode(root),
                "version": version,
                "server": config.SERVER_ID,
                "http": config.PUBLIC_URL,
                "tcp_port": config.TCP_PORT,
            })

        def _openapi(self):
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "docs", "openapi.json")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return self._json(200, json.load(handle))
            except (OSError, ValueError):
                return self._json(404, {"error": "openapi-missing"})

        def _login(self):
            body = self._body()
            account = (body.get("account") or "").strip().lower()
            password = body.get("password") or ""
            record = api.store.account_by_name(account)
            now = store_module.now_iso()
            if record is None or not auth.verify_password(password, record["pass_hash"]):
                api.store.audit("api-login-failed", {"account": account})
                return self._json(401, {"error": "bad-credentials"})
            allowed, reason = auth.may_login(record, now)
            if not allowed:
                return self._json(403, {"error": "not-authorized", "reason": reason})
            token, expires = api.issue(record["call"])
            api.store.audit("api-login", {"account": account, "call": record["call"]})
            self._json(200, {
                "ok": True,
                "token": token,
                "call": record["call"],
                "expires": datetime.fromtimestamp(expires, BEIJING).isoformat(timespec="seconds"),
            })

        def _token_ok(self):
            token = self._bearer()
            call = api.owner(token)
            if not call:
                self._json(401, {"error": "unauthorized"})
                return None
            if not api.rate_ok(token):
                self._json(429, {"error": "rate-limited"})
                return None
            return call

        def _stations(self, query):
            if self._token_ok() is None:
                return
            params, error = _list_params(query, default_limit=500)
            if error:
                return self._json(400, {"error": error})
            rows = api.store.stations(window_hours=params["hours"],
                                      since=params["since"], bbox=params["bbox"],
                                      limit=params["limit"], offset=params["offset"])
            self._json(200, {
                "window": "%dh" % params["hours"],
                "updated": store_module.now_iso(),
                "count": len(rows),
                "total": api.store.station_count(params["hours"]),
                "offset": params["offset"],
                "limit": params["limit"],
                "stations": rows,
            })

        # ── 公开接口：台站页用的，两处都不验 token，按 IP 限流。默认不对外。──
        def _public_stations(self, query):
            if not config.PUBLIC_API:
                return self._json(403, {"error": "not-public"})
            if not api.public_ok(self.client_address[0]):
                return self._json(429, {"error": "rate-limited"})
            params, error = _list_params(query, default_limit=5000)
            if error:
                return self._json(400, {"error": error})
            rows = api.store.stations(window_hours=params["hours"],
                                      since=params["since"], bbox=params["bbox"],
                                      limit=params["limit"], offset=params["offset"])
            self._json(200, {
                "window": "%dh" % params["hours"],
                "updated": store_module.now_iso(),
                "count": len(rows),
                "total": api.store.station_count(params["hours"]),
                "offset": params["offset"],
                "limit": params["limit"],
                "stations": rows,
            })

        def _public_station(self, call):
            if not config.PUBLIC_API:
                return self._json(403, {"error": "not-public"})
            if not api.public_ok(self.client_address[0]):
                return self._json(429, {"error": "rate-limited"})
            data = api.store.station(call)
            if data is None:
                return self._json(404, {"error": "not-found"})
            self._json(200, data)

        def _station(self, call):
            if self._token_ok() is None:
                return
            data = api.store.station(call)
            if data is None:
                return self._json(404, {"error": "not-found"})
            self._json(200, data)

        # ── 管理 ──
        def _admin_state(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            now = store_module.now_iso()
            accounts = api.store.accounts()
            by_status = {}
            with_password = 0
            for row in accounts:
                row["effective"] = auth.effective_status(
                    row["status"], row["valid_from"], row["valid_to"], now)
                row["has_password"] = bool(row.get("pass_hash"))
                row.pop("pass_hash", None)
                key = row["effective"]
                by_status[key] = by_status.get(key, 0) + 1
                if row["has_password"]:
                    with_password += 1
            self._json(200, {
                "ok": True,
                "stats": api.hub.stats(),
                "accounts": accounts,
                "audits": api.store.audits(120),
                "meta": {
                    "whitelist_version": api.store.whitelist_version(),
                    "last_sync": api.store.meta("whitelist_synced_at"),
                    "retention_hours": config.RETENTION_HOURS,
                    "passcode_mode": config.PASSCODE_MODE,
                    "src_policy": config.SRC_POLICY,
                    "auth_mode": config.AUTH_MODE,
                    "station_count": api.store.station_count(),
                    "public_url": config.PUBLIC_URL,
                    "whitelist_synced_at": api.store.meta("whitelist_synced_at"),
                    "whitelist_max_age_hours": config.WHITELIST_MAX_AGE_HOURS,
                    "whitelist": {
                        "version": api.store.whitelist_version(),
                        "synced_at": api.store.meta("whitelist_synced_at"),
                        "max_age_hours": config.WHITELIST_MAX_AGE_HOURS,
                        "entries": len(accounts),
                        "with_password": with_password,
                        "by_status": by_status,
                    },
                    "admin_denied": api.admin_denied,
                    "default_filter": "all",
                    "public_api": config.PUBLIC_API,
                    "archive": {"enabled": config.ARCHIVE_ENABLED,
                                "hours": config.ARCHIVE_HOURS,
                                "lines": api.store.archive_count()},
                    "ingest": api.ingest.stats() if api.ingest else {"enabled": False},
                },
                "time": now,
            })

        def _admin_clients(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            stats = api.hub.stats()
            self._json(200, {"ok": True, "count": len(stats["clients"]),
                             "clients": stats["clients"], "max_conn": config.MAX_CONN})

        def _admin_kick(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            call = (body.get("call") or "").upper().strip()
            if not call:
                return self._json(400, {"error": "bad-call"})
            kicked = api.hub.kick(call)
            self._json(200, {"ok": True, "call": call, "kicked": kicked})

        def _admin_stations(self, query):
            """24 小时台站列表。台站数据不对外，控制台靠管理口令看。"""
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            params, error = _list_params(query, default_limit=500)
            if error:
                return self._json(400, {"error": error})
            rows = api.store.stations(window_hours=params["hours"],
                                      since=params["since"], bbox=params["bbox"],
                                      limit=params["limit"], offset=params["offset"])
            self._json(200, {
                "ok": True,
                "window": "%dh" % params["hours"],
                "updated": store_module.now_iso(),
                "count": len(rows),
                "total": api.store.station_count(params["hours"]),
                "offset": params["offset"],
                "limit": params["limit"],
                "stations": rows,
            })

        def _admin_devices(self, query):
            """台站设备识别排行：按目的呼号（TOCALL）归堆，数台站数。

            一个台站只算最后一条位置包上的目的呼号；认不出的按原字样列出，
            不猜。
            """
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            params, error = _list_params(query, default_limit=60)
            if error:
                return self._json(400, {"error": error})
            rows, total = api.store.device_ranking(window_hours=params["hours"],
                                                   limit=60)
            buckets = {}
            for row in rows:
                to_call = row["to_call"]
                model, cls = tocalls_module.find(to_call)
                # 表里写着「未标明设备」或者压根认不出来时，就写目的呼号本身：
                # 把一堆不同的设备合进一个「未识别」堆里看不出东西。
                if model in ("", "未标明设备"):
                    model = to_call or "未标明"
                    cls = ""
                    identified = False
                else:
                    identified = True
                item = buckets.get(model)
                if item is None:
                    item = buckets[model] = {"model": model, "cls": cls,
                                             "to_calls": [],
                                             "stations": 0, "packets": 0,
                                             "identified": identified}
                item["stations"] += row["stations"]
                item["packets"] += row["packets"]
                item["to_calls"].append(to_call)
            out = sorted(buckets.values(),
                         key=lambda item: (-item["stations"], item["model"]))
            named = sum(item["stations"] for item in out if item["identified"])
            # 小分类：按设备类别拢一下，让「手机应用 78 / 追踪器 40」这种一眼看得见
            classes = {}
            for item in out:
                key = item["cls"] or "未识别"
                classes[key] = classes.get(key, 0) + item["stations"]
            self._json(200, {
                "ok": True,
                "window": "%dh" % params["hours"],
                "updated": store_module.now_iso(),
                "total_stations": total,
                "identified": named,
                "count": len(out),
                "classes": [{"cls": key, "stations": value}
                            for key, value in
                            sorted(classes.items(), key=lambda kv: -kv[1])],
                "devices": out,
            })

        def _admin_station(self, call):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            data = api.store.station(call)
            if data is None:
                return self._json(404, {"error": "not-found"})
            self._json(200, data)

        def _admin_packets(self, query):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            source = "recent"
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                return self._json(400, {"error": "bad-limit"})
            lines = api.hub.lines((query.get("q") or [""])[0],
                                  (query.get("dir") or [""])[0], limit)
            self._json(200, {"ok": True, "source": source, "count": len(lines),
                             "capacity": config.RECENT_LINES, "lines": lines})

        def _admin_archive(self, query):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                return self._json(400, {"error": "bad-limit"})
            lines = api.store.archive_search((query.get("q") or [""])[0],
                                             (query.get("dir") or [""])[0], limit)
            self._json(200, {"ok": True, "source": "archive", "enabled": config.ARCHIVE_ENABLED,
                             "hours": config.ARCHIVE_HOURS, "total": api.store.archive_count(),
                             "count": len(lines), "lines": lines})

        def _admin_audits(self, query):
            """审计列表：可筛类型、可搜关键词、可按游标往后翻。"""
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            kind = (query.get("kind") or [""])[0].strip()
            text = (query.get("q") or [""])[0].strip()
            try:
                limit = int((query.get("limit") or ["60"])[0])
            except ValueError:
                return self._json(400, {"error": "bad-limit"})
            limit = max(1, min(500, limit))
            before = None
            raw = (query.get("before") or [""])[0].strip()
            if raw:
                try:
                    before = int(raw)
                except ValueError:
                    return self._json(400, {"error": "bad-before"})
            rows = api.store.audits(limit=limit, kind=kind or None,
                                    q=text or None, before=before)
            total = api.store.audit_count(kind or None, text or None)
            self._json(200, {
                "ok": True,
                "count": len(rows),
                "total": total,
                "kind": kind,
                "q": text,
                "kinds": api.store.audit_kinds(),
                "next_before": rows[-1]["id"] if rows else None,
                "more": bool(rows) and total > len(rows),
                "rows": rows,
            })

        def _admin_account(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            call = (body.get("call") or "").upper().strip()
            if not call:
                return self._json(400, {"error": "bad-call"})
            current = api.store.account(call) or {}
            password = body.get("password") or ""
            pass_hash = current.get("pass_hash") or ""
            if password:
                pass_hash = auth.hash_password(password)
            elif body.get("clear_password"):
                pass_hash = ""
            record = api.store.save_account(
                call,
                account=(body.get("account") if body.get("account") is not None
                         else current.get("account") or ""),
                pass_hash=pass_hash,
                status=(body.get("status") or current.get("status") or "pending"),
                valid_from=(body.get("valid_from") if body.get("valid_from") is not None
                            else current.get("valid_from")),
                valid_to=(body.get("valid_to") if body.get("valid_to") is not None
                          else current.get("valid_to")),
                note=(body.get("note") if body.get("note") is not None
                      else current.get("note")),
            )
            version = api.store.bump_whitelist_version()
            api.store.audit("account-saved", {"call": call, "version": version})
            dropped = api.enforce_whitelist("account-saved")
            record.pop("pass_hash", None)
            self._json(200, {"ok": True, "account": record, "version": version,
                             "dropped": dropped})

        def _admin_delete(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            call = (body.get("call") or "").upper().strip()
            removed = api.store.delete_account(call)
            version = api.store.bump_whitelist_version()
            api.store.audit("account-deleted", {"call": call, "removed": removed,
                                                "version": version})
            dropped = api.enforce_whitelist("account-deleted")
            self._json(200, {"ok": True, "removed": removed, "version": version,
                             "dropped": dropped})

        def _admin_whitelist(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            payload = {
                "mode": "full",
                "version": api.store.whitelist_version(),
                "updated": store_module.now_iso(),
                "records": api.store.whitelist_records(),
            }
            self._json(200, payload)

        def _admin_publish(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            version = api.store.bump_whitelist_version()
            api.store.set_meta("whitelist_synced_at", store_module.now_iso())
            api.store.audit("whitelist-published", {"version": version})
            self._json(200, {"ok": True, "version": version})

        def _manager_status(self):
            self._json(200, {
                "ok": True,
                "version": api.store.whitelist_version(),
                "last_sync": api.store.meta("whitelist_synced_at"),
                "records": len(api.store.accounts()),
            })

        def _manager_publish(self):
            if not config.MANAGER_SECRET:
                return self._json(403, {"error": "manager-disabled"})
            body = self._body()
            signature = body.pop("sig", "")
            if not auth.verify_signature(body, signature, config.MANAGER_SECRET):
                api.store.audit("whitelist-rejected", {"reason": "bad-signature"})
                return self._json(403, {"error": "bad-signature"})
            try:
                version = int(body.get("version") or 0)
            except (TypeError, ValueError):
                return self._json(400, {"error": "bad-version"})
            current = api.store.whitelist_version()
            if version <= current:
                return self._json(409, {"error": "stale-version",
                                        "version": current})
            count, mode = api.store.apply_whitelist(body.get("records") or [],
                                                    body.get("mode") or "full")
            api.store.set_meta("whitelist_version", str(version))
            api.store.set_meta("whitelist_synced_at", store_module.now_iso())
            api.store.audit("whitelist-synced", {"version": version, "mode": mode,
                                                 "records": count})
            dropped = api.enforce_whitelist("whitelist-synced")
            self._json(200, {"ok": True, "version": version, "records": count,
                             "mode": mode, "dropped": dropped})

        # ── 个人数据同步（消息 / 备份 / 历史轨迹）──
        # 一个呼号 + 一个长期令牌，三类数据都走它的。令牌落库只存哈希，
        # 撤销是按呼号或按设备整批置位，不是删行（留痕）。
        def _sync_record(self):
            """认令牌。认不下来已经自己答了 401/429，返回 None。"""
            if not config.SYNC_ENABLED:
                self._json(403, {"error": "sync-disabled"})
                return None
            token = self._bearer()
            record = api.sync_owner(token)
            if not record:
                self._json(401, {"error": "unauthorized"})
                return None
            if not api.rate_ok(token):
                self._json(429, {"error": "rate-limited"})
                return None
            return record

        def _sync_login(self):
            """口令换长期令牌；顺手记下这台设备（安装指纹 + 版本 + 平台）。"""
            body = self._body()
            account = (body.get("account") or "").strip().lower()
            password = body.get("password") or ""
            device = body.get("device") if isinstance(body.get("device"), dict) else {}
            record = api.store.account_by_name(account)
            now = store_module.now_iso()
            if record is None or not auth.verify_password(password, record["pass_hash"]):
                api.store.audit("sync-login-failed", {"account": account})
                return self._json(401, {"error": "bad-credentials"})
            allowed, reason = auth.may_login(record, now)
            if not allowed:
                return self._json(403, {"error": "not-authorized", "reason": reason})
            call = record["call"]
            did = (device.get("id") or "").strip()[:64]
            api.store.sync_device(call, did, device.get("ver") or "",
                                  device.get("platform") or "", device.get("abi") or "")
            token, expires = api.sync_issue(call, did)
            api.store.audit("sync-login", {"account": account, "call": call,
                                           "device": did})
            self._json(200, {
                "ok": True,
                "token": token,
                "call": call,
                "device": did,
                "expires": datetime.fromtimestamp(expires, BEIJING).isoformat(timespec="seconds"),
            })

        def _sync_logout(self):
            record = self._sync_record()
            if not record:
                return
            api.store.sync_revoke(record["call"], token_hash=record["token_hash"])
            self._json(200, {"ok": True})

        def _sync_state(self):
            record = self._sync_record()
            if not record:
                return
            call = record["call"]
            messages = api.store.sync_messages_meta(call)
            days = api.store.sync_track_days(call)
            self._json(200, {
                "ok": True,
                "call": call,
                "device": record["device"] or "",
                "server_time": store_module.now_iso(),
                "devices": api.store.sync_devices(call),
                "messages": {
                    "count": messages["count"],
                    "last": (store_module.iso_of_ms(messages["max_ts"])
                             if messages["max_ts"] else None),
                },
                "tracks": {
                    "days": len(days),
                    "first": days[-1]["day"] if days else None,
                    "last": days[0]["day"] if days else None,
                    "points": sum(day["points"] for day in days),
                },
                "backups": api.store.sync_backup_list(call),
            })

        def _sync_messages_put(self):
            record = self._sync_record()
            if not record:
                return
            body = self._body()
            items = body.get("items")
            if not isinstance(items, list):
                return self._json(400, {"error": "bad-items"})
            cap = max(1, min(1000, config.SYNC_BATCH))
            items = [item for item in items if isinstance(item, dict)][:cap]
            added = api.store.sync_messages_put(record["call"], items,
                                               limit=config.SYNC_MSG_LIMIT)
            meta = api.store.sync_messages_meta(record["call"])
            self._json(200, {"ok": True, "added": added, "count": meta["count"],
                             "last": (store_module.iso_of_ms(meta["max_ts"])
                                      if meta["max_ts"] else None),
                             "server_time": store_module.now_iso()})

        def _sync_messages_list(self, query):
            record = self._sync_record()
            if not record:
                return
            try:
                since = int((query.get("since") or ["0"])[0] or 0)
                # 读的页比写的大：历史整份拉回来时少翻几趟
                limit = max(1, min(2000,
                                   int((query.get("limit") or ["500"])[0] or 500)))
                offset = max(0, int((query.get("offset") or ["0"])[0] or 0))
            except ValueError:
                return self._json(400, {"error": "bad-params"})
            rows = api.store.sync_messages(record["call"], since=since,
                                           limit=limit, offset=offset)
            meta = api.store.sync_messages_meta(record["call"])
            self._json(200, {"ok": True, "count": len(rows), "messages": rows,
                             "total": meta["count"],
                             "more": bool(rows) and offset + len(rows) < meta["count"],
                             "since": since, "offset": offset, "limit": limit,
                             "server_time": store_module.now_iso()})

        def _sync_track_put(self, day):
            record = self._sync_record()
            if not record:
                return
            if not _day_ok(day):
                return self._json(400, {"error": "bad-day"})
            raw = self._raw()
            if not raw:
                return self._json(400, {"error": "empty-body"})
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "bad-json"})
            points = body.get("points") if isinstance(body, dict) else body
            if not isinstance(points, list):
                return self._json(400, {"error": "bad-points"})
            cleaned = []
            for point in points[:config.SYNC_TRACK_POINTS]:
                if not isinstance(point, dict):
                    continue
                try:
                    stamp = int(point.get("t") or 0)
                except (TypeError, ValueError):
                    continue
                if not stamp:
                    continue
                cleaned.append({key: point[key] for key in
                                ("t", "lat", "lng", "v", "c", "a", "acc", "hr")
                                if key in point})
            total = api.store.sync_track_put(record["call"], day, cleaned,
                                             device=record["device"] or "",
                                             limit=config.SYNC_TRACK_POINTS)
            api.store.sync_track_prune(record["call"], keep_days=config.SYNC_TRACK_DAYS)
            self._json(200, {"ok": True, "day": day, "points": total,
                             "device": record["device"] or "",
                             "sent": len(cleaned),
                             "server_time": store_module.now_iso()})

        def _sync_track_days(self):
            record = self._sync_record()
            if not record:
                return
            self._json(200, {"ok": True, "days": api.store.sync_track_days(record["call"]),
                             "server_time": store_module.now_iso()})

        def _sync_track(self, day):
            record = self._sync_record()
            if not record:
                return
            if not _day_ok(day):
                return self._json(400, {"error": "bad-day"})
            query = parse_qs(urlparse(self.path).query)
            device = (query.get("device") or [""])[0]
            points = api.store.sync_track(record["call"], day,
                                          device=device or None)
            if points is None:
                return self._json(404, {"error": "not-found"})
            self._json(200, {"ok": True, "day": day,
                             "device": device or None, "points": points,
                             "devices": api.store.sync_track_devices(record["call"], day)})

        def _sync_track_delete(self, day):
            record = self._sync_record()
            if not record:
                return
            query = parse_qs(urlparse(self.path).query)
            device = (query.get("device") or [""])[0]
            removed = api.store.sync_track_delete(record["call"], day,
                                                  device=device or None)
            self._json(200, {"ok": True, "day": day, "device": device or None,
                             "removed": removed})

        def _sync_backup_put(self, query):
            """上传一份备份。请求体就是那份备份本身（JSON），不再套一层。"""
            record = self._sync_record()
            if not record:
                return
            raw = self._raw()
            if not raw:
                return self._json(400, {"error": "empty-body"})
            try:
                text = raw.decode("utf-8")
                doc = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "bad-json"})
            if not isinstance(doc, dict):
                return self._json(400, {"error": "bad-backup"})
            label = (query.get("label") or [""])[0][:48]
            info = api.store.sync_backup_put(record["call"], text, label=label,
                                             keep=config.SYNC_BACKUPS)
            api.store.audit("sync-backup", {"call": record["call"],
                                            "id": info["id"], "size": info["size"]})
            self._json(200, {"ok": True, "backup": info,
                             "backups": api.store.sync_backup_list(record["call"])})

        def _sync_backup_list(self):
            record = self._sync_record()
            if not record:
                return
            self._json(200, {"ok": True,
                             "backups": api.store.sync_backup_list(record["call"]),
                             "server_time": store_module.now_iso()})

        def _sync_backup(self, bid):
            record = self._sync_record()
            if not record:
                return
            text = api.store.sync_backup_get(record["call"], bid)
            if text is None:
                return self._json(404, {"error": "not-found"})
            try:
                return self._json(200, json.loads(text))
            except ValueError:
                return self._json(500, {"error": "broken-backup"})

        def _sync_backup_delete(self, bid):
            record = self._sync_record()
            if not record:
                return
            removed = api.store.sync_backup_delete(record["call"], bid)
            self._json(200, {"ok": True, "removed": removed,
                             "backups": api.store.sync_backup_list(record["call"])})

        def _sync_device_delete(self, device):
            """去掉一台设备：它的令牌全部作废，数据留着。"""
            record = self._sync_record()
            if not record:
                return
            revoked = api.store.sync_drop_device(record["call"], device)
            api.store.audit("sync-device-drop", {"call": record["call"],
                                                 "device": device, "revoked": revoked})
            self._json(200, {"ok": True, "revoked": revoked,
                             "devices": api.store.sync_devices(record["call"])})

        def _sync_clear(self):
            record = self._sync_record()
            if not record:
                return
            removed = api.store.sync_clear(record["call"])
            api.store.audit("sync-clear", {"call": record["call"], "removed": removed})
            self._json(200, {"ok": True, "removed": removed})

        def _admin_sync(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            self._json(200, {"ok": True, "rows": api.store.sync_overview(),
                             "enabled": config.SYNC_ENABLED,
                             "time": store_module.now_iso()})

        # ── 设置 ──
        def _admin_settings(self):
            """能在控制台里直接改的运行参数：当前值、来源（库或 env）、可选项。"""
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            self._json(200, {
                "ok": True,
                "groups": list(settings_module.GROUPS),
                "rows": settings_module.rows(api.store),
                "restart_only": settings_module.restart_rows(),
                "time": store_module.now_iso(),
            })

        def _admin_settings_save(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            values = body.get("values") if isinstance(body.get("values"), dict) else body
            result = settings_module.save(api.store, api, values or {})
            if not result.get("ok"):
                return self._json(400, result)
            self._json(200, dict(result, groups=list(settings_module.GROUPS),
                                 rows=settings_module.rows(api.store),
                                 restart_only=settings_module.restart_rows(),
                                 time=store_module.now_iso()))

        def _admin_settings_reset(self):
            """退回 env 里的值：带 keys 只退那几项，不带就全退。"""
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            keys = body.get("keys")
            if not isinstance(keys, list) or not keys:
                keys = None
            result = settings_module.reset(api.store, api, keys)
            if not result.get("ok"):
                return self._json(400, result)
            self._json(200, dict(result, groups=list(settings_module.GROUPS),
                                 rows=settings_module.rows(api.store),
                                 restart_only=settings_module.restart_rows(),
                                 time=store_module.now_iso()))

    return Handler
