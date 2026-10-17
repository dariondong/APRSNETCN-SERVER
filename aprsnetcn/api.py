"""HTTP：公开的 24 小时台站 API + MANAGER 管理接口 + 一个控制台页面。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, config, hub as hub_module, store as store_module

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
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

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
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1 << 20:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _bearer(self):
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return ""

        def _admin_ok(self):
            if not config.ADMIN_PASSWORD:
                return False
            return auth.constant_equal(self.headers.get("X-ACN-Token"), config.ADMIN_PASSWORD)

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
                if path == "/api/v1/openapi.json":
                    return self._openapi()
                if path == "/stations":
                    return self._stations_page()
                if path == "/assets/coast.json":
                    return self._asset("coast.json", "application/json; charset=utf-8")
                if path == "/api/public/stations":
                    return self._public_stations(query)
                if path.startswith("/api/public/stations/"):
                    return self._public_station(path.rsplit("/", 1)[-1])
                if path == "/api/v1/stations":
                    return self._stations(query)
                if path.startswith("/api/v1/stations/"):
                    return self._station(path.rsplit("/", 1)[-1])
                if path == "/api/admin/state":
                    return self._admin_state()
                if path == "/api/admin/whitelist":
                    return self._admin_whitelist()
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
                if path == "/api/admin/account":
                    return self._admin_account()
                if path == "/api/admin/account/delete":
                    return self._admin_delete()
                if path == "/api/admin/publish":
                    return self._admin_publish()
                if path == "/api/manager/whitelist":
                    return self._manager_publish()
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
            """公开的 24 小时台站页：地图 + 点选详情，不用 token 也不用登录。"""
            path = os.path.join(os.path.dirname(__file__), "stations.html")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return self._html(200, handle.read())
            except OSError:
                return self._json(404, {"error": "page-missing"})

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
                "login_extensions": ["vers", "auth", "filter"],
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
                "stations": rows,
            })

        # ── 公开接口：台站页用的，两处都不验 token，按 IP 限流 ──
        def _public_stations(self, query):
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
                "stations": rows,
            })

        def _public_station(self, call):
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
            for row in accounts:
                row["effective"] = auth.effective_status(
                    row["status"], row["valid_from"], row["valid_to"], now)
                row.pop("pass_hash", None)
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
                    "station_count": api.store.station_count(),
                    "public_url": config.PUBLIC_URL,
                    "ingest": api.ingest.stats() if api.ingest else {"enabled": False},
                },
                "time": now,
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
            record.pop("pass_hash", None)
            self._json(200, {"ok": True, "account": record, "version": version})

        def _admin_delete(self):
            if not self._admin_ok():
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            call = (body.get("call") or "").upper().strip()
            removed = api.store.delete_account(call)
            version = api.store.bump_whitelist_version()
            api.store.audit("account-deleted", {"call": call, "removed": removed,
                                                "version": version})
            self._json(200, {"ok": True, "removed": removed, "version": version})

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
            self._json(200, {"ok": True, "version": version, "records": count,
                             "mode": mode})

    return Handler
