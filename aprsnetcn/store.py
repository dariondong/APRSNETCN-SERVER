"""SQLite 存储：账号与名单、24 小时位置、审计与元信息。"""

from __future__ import annotations

import gzip
import json
import math
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accounts (
        call TEXT PRIMARY KEY,
        account TEXT,
        pass_hash TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        valid_from TEXT,
        valid_to TEXT,
        note TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        updated TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call TEXT NOT NULL,
        ts INTEGER NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        sym_table TEXT,
        sym_code TEXT,
        speed REAL,
        course REAL,
        alt REAL,
        comment TEXT,
        dst TEXT,
        via TEXT,
        src_call TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts)",
    "CREATE INDEX IF NOT EXISTS positions_call_ts ON positions(call, ts)",
    """CREATE TABLE IF NOT EXISTS archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        dir TEXT NOT NULL,
        src TEXT,
        dst TEXT,
        line TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS archive_ts ON archive(ts)",
    "CREATE INDEX IF NOT EXISTS archive_src_ts ON archive(src, ts)",
    """CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    # ── 个人数据同步：令牌、设备、消息、轨迹、备份 ──
    # 令牌只存哈希：库被人拿走也拿不到能用的令牌。
    """CREATE TABLE IF NOT EXISTS sync_tokens (
        token_hash TEXT PRIMARY KEY,
        call TEXT NOT NULL,
        device TEXT,
        created INTEGER NOT NULL,
        used INTEGER,
        expires INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS sync_tokens_call ON sync_tokens(call)",
    """CREATE TABLE IF NOT EXISTS sync_devices (
        call TEXT NOT NULL,
        device TEXT NOT NULL,
        ver TEXT,
        platform TEXT,
        abi TEXT,
        first INTEGER,
        last INTEGER,
        PRIMARY KEY (call, device)
    )""",
    """CREATE TABLE IF NOT EXISTS user_messages (
        call TEXT NOT NULL,
        mid TEXT NOT NULL,
        ts INTEGER NOT NULL,
        peer TEXT,
        dir TEXT,
        text TEXT,
        acked INTEGER NOT NULL DEFAULT 0,
        grp TEXT,
        PRIMARY KEY (call, mid)
    )""",
    "CREATE INDEX IF NOT EXISTS user_messages_ts ON user_messages(call, ts)",
    """CREATE TABLE IF NOT EXISTS user_tracks (
        call TEXT NOT NULL,
        day TEXT NOT NULL,
        device TEXT NOT NULL DEFAULT '',
        points INTEGER NOT NULL DEFAULT 0,
        updated INTEGER,
        payload BLOB,
        PRIMARY KEY (call, day, device)
    )""",
    """CREATE TABLE IF NOT EXISTS user_backups (
        call TEXT NOT NULL,
        id TEXT NOT NULL,
        created INTEGER,
        label TEXT,
        size INTEGER,
        payload BLOB,
        PRIMARY KEY (call, id)
    )""",
)


def now_iso():
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def iso_of(epoch):
    return datetime.fromtimestamp(epoch, BEIJING).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            for statement in SCHEMA:
                self._db.execute(statement)
            self._migrate()
            self._db.commit()

    def _migrate(self):
        """旧库补列。SQLite 3.31 起才支持 DROP COLUMN，所以只增不删。"""
        columns = {row["name"] for row in
                   self._db.execute("PRAGMA table_info(positions)").fetchall()}
        if "dst" not in columns:
            self._db.execute("ALTER TABLE positions ADD COLUMN dst TEXT")

    # ── 账号与名单 ──
    def account(self, call):
        with self._lock:
            row = self._db.execute("SELECT * FROM accounts WHERE call = ?",
                                   ((call or "").upper(),)).fetchone()
        return dict(row) if row else None

    def account_by_name(self, account):
        if not account:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM accounts WHERE lower(account) = ?",
                                   (account.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def accounts(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM accounts ORDER BY status, call").fetchall()
        return [dict(row) for row in rows]

    def save_account(self, call, account=None, pass_hash=None, status="pending",
                     valid_from=None, valid_to=None, note=None, bump_version=True):
        call = (call or "").upper().strip()
        if not call:
            raise ValueError("call")
        with self._lock:
            current = self._db.execute(
                "SELECT version FROM accounts WHERE call = ?", (call,)).fetchone()
            version = (current["version"] if current else 0) + (1 if bump_version else 0)
            self._db.execute(
                """INSERT INTO accounts (call, account, pass_hash, status, valid_from,
                       valid_to, note, version, updated)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(call) DO UPDATE SET
                       account = excluded.account,
                       pass_hash = excluded.pass_hash,
                       status = excluded.status,
                       valid_from = excluded.valid_from,
                       valid_to = excluded.valid_to,
                       note = excluded.note,
                       version = excluded.version,
                       updated = excluded.updated""",
                (call, (account or "").strip().lower(), pass_hash or "", status,
                 valid_from, valid_to, note, version, now_iso()))
            self._db.commit()
        return self.account(call)

    def delete_account(self, call):
        with self._lock:
            cursor = self._db.execute("DELETE FROM accounts WHERE call = ?",
                                      ((call or "").upper(),))
            self._db.commit()
        return cursor.rowcount > 0

    def whitelist_version(self):
        value = self.meta("whitelist_version")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def bump_whitelist_version(self):
        version = self.whitelist_version() + 1
        self.set_meta("whitelist_version", str(version))
        return version

    def apply_whitelist(self, records, mode="full"):
        """整体替换或按条合并。返回应用后的条数与版本。"""
        mode = "incremental" if str(mode).lower().startswith("inc") else "full"
        with self._lock:
            if mode == "full":
                self._db.execute("DELETE FROM accounts")
            for record in records:
                call = (record.get("call") or "").upper().strip()
                if not call:
                    continue
                self._db.execute(
                    """INSERT INTO accounts (call, account, pass_hash, status,
                           valid_from, valid_to, note, version, updated)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(call) DO UPDATE SET
                           account = excluded.account,
                           pass_hash = CASE WHEN excluded.pass_hash != ''
                                            THEN excluded.pass_hash
                                            ELSE accounts.pass_hash END,
                           status = excluded.status,
                           valid_from = excluded.valid_from,
                           valid_to = excluded.valid_to,
                           note = excluded.note,
                           version = excluded.version,
                           updated = excluded.updated""",
                    (call, record.get("account") or "", record.get("pass_hash") or "",
                     (record.get("status") or "pending").lower(),
                     record.get("valid_from"), record.get("valid_to"),
                     record.get("note"), int(record.get("version") or 0), now_iso()))
            self._db.commit()
        return len(records), mode

    def whitelist_records(self, include_hash=True):
        records = []
        for row in self.accounts():
            record = {
                "call": row["call"],
                "account": row["account"] or "",
                "status": row["status"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "note": row["note"],
                "version": row["version"],
            }
            if include_hash:
                record["pass_hash"] = row["pass_hash"] or ""
            records.append(record)
        return records

    # ── 24 小时位置 ──
    def record_position(self, pkt, via="is", src_call="", now=None):
        if pkt is None or pkt.lat is None or pkt.lon is None:
            return False
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                """INSERT INTO positions (call, ts, lat, lon, sym_table, sym_code,
                       speed, course, alt, comment, dst, via, src_call)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pkt.src, stamp, pkt.lat, pkt.lon, pkt.sym_table, pkt.sym_code,
                 pkt.speed, pkt.course, pkt.alt, (pkt.comment or "")[:160],
                 (getattr(pkt, "dst", "") or "")[:12], via, src_call))
            self._db.commit()
        return True

    def purge(self, hours=24, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(hours * 3600)
        with self._lock:
            cursor = self._db.execute("DELETE FROM positions WHERE ts < ?", (edge,))
            self._db.commit()
        return cursor.rowcount

    # ── 报文存档（默认关，见 config.ARCHIVE_ENABLED）──
    def archive(self, entry, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                "INSERT INTO archive (ts, dir, src, dst, line) VALUES (?,?,?,?,?)",
                (stamp, entry.get("dir") or "in", (entry.get("src") or "")[:24],
                 (entry.get("dst") or "")[:16], (entry.get("line") or "")[:512]))
            self._db.commit()

    def purge_archive(self, hours=72, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(hours * 3600)
        with self._lock:
            cursor = self._db.execute("DELETE FROM archive WHERE ts < ?", (edge,))
            self._db.commit()
        return cursor.rowcount

    def archive_search(self, query="", direction="", limit=200, now=None):
        direction = (direction or "").lower()
        sql = ["SELECT * FROM archive WHERE 1=1"]
        args = []
        if direction in ("in", "out"):
            sql.append("AND dir = ?")
            args.append(direction)
        needle = (query or "").strip()
        if needle:
            sql.append("AND (line LIKE ? OR src LIKE ? OR dst LIKE ?)")
            args.extend(["%" + needle + "%"] * 3)
        sql.append("ORDER BY id DESC LIMIT ?")
        args.append(max(1, min(500, int(limit or 200))))
        with self._lock:
            rows = self._db.execute(" ".join(sql), args).fetchall()
        return [{"id": row["id"], "time": iso_of(row["ts"]), "dir": row["dir"],
                 "src": row["src"] or "", "dst": row["dst"] or "",
                 "line": row["line"] or ""} for row in rows]

    def archive_count(self, now=None):
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM archive").fetchone()
        return row["n"] if row else 0

    def quick_check(self):
        """启动自检用：SQLite 自己查一遍库，正常返回 ok。"""
        with self._lock:
            row = self._db.execute("PRAGMA quick_check").fetchone()
        return (row[0] if row else "unknown")

    def stations(self, window_hours=24, since=None, bbox=None, limit=500,
                 offset=0, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = int(since) if since else stamp - int(window_hours * 3600)
        sql = ["SELECT call, MAX(ts) AS last_ts, COUNT(*) AS packets",
               "FROM positions WHERE ts >= ?"]
        args = [edge]
        if bbox:
            sql.append("AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            args.extend([bbox[0], bbox[2], bbox[1], bbox[3]])
        sql.append("GROUP BY call ORDER BY last_ts DESC, call ASC LIMIT ? OFFSET ?")
        args.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._db.execute(" ".join(sql), args).fetchall()
            result = []
            for row in rows:
                last = self._db.execute(
                    "SELECT * FROM positions WHERE call = ? AND ts = ? ORDER BY id DESC LIMIT 1",
                    (row["call"], row["last_ts"])).fetchone()
                result.append(_station_dict(row["call"], last, row["packets"]))
        return result

    def station(self, call, window_hours=24, now=None):
        call = (call or "").upper()
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(window_hours * 3600)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM positions WHERE call = ? AND ts >= ? ORDER BY ts",
                (call, edge)).fetchall()
        if not rows:
            return None
        last = rows[-1]
        data = _station_dict(call, last, len(rows))
        data["track"] = [{"time": iso_of(row["ts"]), "lat": row["lat"], "lon": row["lon"],
                          "speed": row["speed"], "course": row["course"]}
                         for row in rows]
        data["first_seen"] = iso_of(rows[0]["ts"])
        return data

    def station_count(self, window_hours=24, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(window_hours * 3600)
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(DISTINCT call) AS n FROM positions WHERE ts >= ?",
                (edge,)).fetchone()
        return row["n"] if row else 0

    def device_ranking(self, window_hours=24, limit=40, now=None):
        """每个台站取窗口内**最后一条**位置包的目的呼号，再按目的呼号归堆。

        SQLite 的 bare-column + MAX() 就是「取 MAX 那一行的其它列」，正好是
        要的语义：一个台站 24 小时内换了设备，只算最后一次。
        """
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(window_hours * 3600)
        with self._lock:
            rows = self._db.execute(
                """SELECT dst, COUNT(*) AS stations, SUM(packets) AS packets
                     FROM (
                       SELECT call, dst, MAX(ts) AS last_ts, COUNT(*) AS packets
                         FROM positions WHERE ts >= ? AND dst IS NOT NULL
                        GROUP BY call
                     )
                    GROUP BY dst
                    ORDER BY stations DESC, dst ASC
                    LIMIT ?""",
                (edge, int(limit))).fetchall()
            total = self._db.execute(
                "SELECT COUNT(DISTINCT call) AS n FROM positions WHERE ts >= ?",
                (edge,)).fetchone()["n"]
        return [{"to_call": row["dst"] or "",
                 "stations": row["stations"],
                 "packets": row["packets"] or 0} for row in rows], total

    def position_count(self, window_hours=24, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(window_hours * 3600)
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM positions WHERE ts >= ?",
                                   (edge,)).fetchone()
        return row["n"] if row else 0

    # ── 个人数据同步：令牌与设备 ──
    def sync_put_token(self, token_hash, call, device, ttl, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sync_tokens "
                "(token_hash, call, device, created, used, expires, revoked) "
                "VALUES (?,?,?,?,?,?,0)",
                (token_hash, call, device or "", stamp, stamp, stamp + int(ttl)))
            self._db.commit()
        return stamp + int(ttl)

    def sync_token(self, token_hash, now=None):
        """认令牌。过期的、被撤的一律当没有；认下来顺手记一次 used。"""
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            row = self._db.execute("SELECT * FROM sync_tokens WHERE token_hash = ?",
                                   (token_hash,)).fetchone()
            if not row or row["revoked"] or row["expires"] < stamp:
                return None
            self._db.execute("UPDATE sync_tokens SET used = ? WHERE token_hash = ?",
                             (stamp, token_hash))
            self._db.commit()
        return dict(row)

    def sync_tokens(self, call):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sync_tokens WHERE call = ? ORDER BY created DESC",
                (call,)).fetchall()
        return [{"device": row["device"] or "", "created": iso_of(row["created"]),
                 "used": iso_of(row["used"]) if row["used"] else None,
                 "expires": iso_of(row["expires"]),
                 "revoked": bool(row["revoked"])} for row in rows]

    def sync_revoke(self, call, device=None, token_hash=None):
        sql = "UPDATE sync_tokens SET revoked = 1 WHERE call = ? AND revoked = 0"
        args = [call]
        if token_hash:
            sql += " AND token_hash = ?"
            args.append(token_hash)
        if device:
            sql += " AND device = ?"
            args.append(device)
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
        return cursor.rowcount

    def sync_device(self, call, device, ver="", platform="", abi="", now=None):
        """记一台设备：先到的写 first，每次都刷新 last / 版本。"""
        device = (device or "").strip()
        if not device:
            return False
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._db.execute(
                """INSERT INTO sync_devices (call, device, ver, platform, abi, first, last)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(call, device) DO UPDATE SET
                       ver = excluded.ver,
                       platform = excluded.platform,
                       abi = excluded.abi,
                       last = excluded.last""",
                (call, device, (ver or "")[:24], (platform or "")[:24],
                 (abi or "")[:24], stamp, stamp))
            self._db.commit()
        return True

    def sync_devices(self, call):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sync_devices WHERE call = ? ORDER BY last DESC",
                (call,)).fetchall()
        return [{"device": row["device"], "ver": row["ver"] or "",
                 "platform": row["platform"] or "", "abi": row["abi"] or "",
                 "first": iso_of(row["first"]) if row["first"] else None,
                 "last": iso_of(row["last"]) if row["last"] else None} for row in rows]

    def sync_drop_device(self, call, device):
        """去掉一台设备：它发过的令牌一起作废，数据留着（数据属于呼号）。"""
        with self._lock:
            self._db.execute("DELETE FROM sync_devices WHERE call = ? AND device = ?",
                             (call, device))
            self._db.commit()
        return self.sync_revoke(call, device=device)

    # ── 个人数据同步：消息 ──
    def sync_messages_put(self, call, items, limit=5000, now=None):
        """并集写：新 mid 插入，已存在的只把 acked 往「已确认」上推。

        客户端只负责把它手里的一批报上来，去重与截断都在这里，
        这样两台设备各自上传也不会出现重复行。
        """
        added = 0
        with self._lock:
            for item in items:
                mid = (item.get("mid") or "").strip()
                if not mid:
                    continue
                try:
                    ts = int(item.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                row = self._db.execute(
                    "SELECT acked FROM user_messages WHERE call = ? AND mid = ?",
                    (call, mid)).fetchone()
                acked = 1 if item.get("acked") else 0
                if row is None:
                    self._db.execute(
                        """INSERT INTO user_messages
                               (call, mid, ts, peer, dir, text, acked, grp)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (call, mid, ts, (item.get("peer") or "")[:24],
                         (item.get("dir") or "in")[:8],
                         (item.get("text") or "")[:512], acked,
                         (item.get("grp") or "")[:32]))
                    added += 1
                elif acked and not row["acked"]:
                    self._db.execute(
                        "UPDATE user_messages SET acked = 1 "
                        "WHERE call = ? AND mid = ?", (call, mid))
            self._db.execute(
                "DELETE FROM user_messages WHERE call = ? AND mid NOT IN "
                "(SELECT mid FROM user_messages WHERE call = ? "
                " ORDER BY ts DESC, mid DESC LIMIT ?)",
                (call, call, int(limit)))
            self._db.commit()
        return added

    def sync_messages(self, call, since=0, limit=500, offset=0):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM user_messages WHERE call = ? AND ts >= ? "
                "ORDER BY ts ASC, mid ASC LIMIT ? OFFSET ?",
                (call, int(since), int(limit), int(offset))).fetchall()
        return [{"mid": row["mid"], "ts": row["ts"], "peer": row["peer"] or "",
                 "dir": row["dir"] or "in", "text": row["text"] or "",
                 "acked": bool(row["acked"]), "grp": row["grp"] or ""}
                for row in rows]

    def sync_messages_meta(self, call):
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n, MAX(ts) AS last FROM user_messages "
                "WHERE call = ?", (call,)).fetchone()
        return {"count": row["n"] if row else 0,
                "max_ts": row["last"] if row else None}

    # ── 个人数据同步：历史轨迹 ──
    # 一天一档，但按设备分开存：两台设备同一天各走各的，混成一行就没法再分开。
    # 读的时候多台叠在一起，隔得近的点归并成一点（同一趟路两台手机都记了一遍）。
    def sync_track_put(self, call, day, points, device="", limit=60000, now=None):
        """写一台设备的那一天：与库里这一行并集（按时间戳去重）。"""
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        merged = {}
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM user_tracks "
                "WHERE call = ? AND day = ? AND device = ?",
                (call, day, device or "")).fetchone()
            if row and row["payload"]:
                for point in _unpack_points(row["payload"]):
                    merged[int(point.get("t") or 0)] = point
            for point in points:
                try:
                    key = int(point.get("t") or 0)
                except (TypeError, ValueError):
                    continue
                if not key:
                    continue
                old = merged.get(key)
                # 同一秒两条：留字段多的那条（心率 / 海拔只在其中一条时有值）
                if old is None or len(point) > len(old):
                    merged[key] = point
            ordered = [merged[key] for key in sorted(merged)]
            if len(ordered) > int(limit):
                ordered = ordered[:int(limit)]
            self._db.execute(
                """INSERT INTO user_tracks (call, day, device, points, updated, payload)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(call, day, device) DO UPDATE SET
                       points = excluded.points,
                       updated = excluded.updated,
                       payload = excluded.payload""",
                (call, day, device or "", len(ordered), stamp, _pack_points(ordered)))
            self._db.commit()
        return len(ordered)

    def sync_track_days(self, call):
        """一天一行：点数按所有设备合起来算，另外告诉你这天有几台设备。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT day, SUM(points) AS points, COUNT(*) AS devices, "
                "       MAX(updated) AS updated "
                "FROM user_tracks WHERE call = ? GROUP BY day ORDER BY day DESC",
                (call,)).fetchall()
        return [{"day": row["day"], "points": row["points"] or 0,
                 "devices": row["devices"],
                 "updated": iso_of(row["updated"]) if row["updated"] else None}
                for row in rows]

    def sync_track_devices(self, call, day):
        with self._lock:
            rows = self._db.execute(
                "SELECT device, points, updated FROM user_tracks "
                "WHERE call = ? AND day = ? ORDER BY updated DESC",
                (call, day)).fetchall()
        return [{"device": row["device"] or "", "points": row["points"] or 0,
                 "updated": iso_of(row["updated"]) if row["updated"] else None}
                for row in rows]

    def sync_track(self, call, day, device=None):
        """读一天。给 device 就只读那一台，不给就把所有设备合起来去重。"""
        sql = "SELECT payload FROM user_tracks WHERE call = ? AND day = ?"
        args = [call, day]
        if device is not None:
            sql += " AND device = ?"
            args.append(device)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        blobs = [row["payload"] for row in rows if row["payload"]]
        if not blobs:
            return None
        return merge_points(blobs)

    def sync_track_delete(self, call, day, device=None):
        sql = "DELETE FROM user_tracks WHERE call = ? AND day = ?"
        args = [call, day]
        if device is not None:
            sql += " AND device = ?"
            args.append(device)
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
        return cursor.rowcount

    def sync_track_prune(self, call, keep_days=365):
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM user_tracks WHERE call = ? AND day NOT IN "
                "(SELECT day FROM user_tracks WHERE call = ? ORDER BY day DESC LIMIT ?)",
                (call, call, int(keep_days)))
            self._db.commit()
        return cursor.rowcount

    # ── 个人数据同步：云端备份 ──
    def sync_backup_put(self, call, text, label="", keep=3, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        bid = "%d-%04x" % (stamp, secrets.randbelow(0x10000))
        with self._lock:
            self._db.execute(
                "INSERT INTO user_backups (call, id, created, label, size, payload) "
                "VALUES (?,?,?,?,?,?)",
                (call, bid, stamp, (label or "")[:48], len(text),
                 gzip.compress(text.encode("utf-8"))))
            # 同一秒里传几份是常事，按写入顺序而不是时间戳排 —— 否则「最新那份」
            # 会随机变成其中任一份。
            self._db.execute(
                "DELETE FROM user_backups WHERE call = ? AND rowid NOT IN "
                "(SELECT rowid FROM user_backups WHERE call = ? "
                " ORDER BY rowid DESC LIMIT ?)",
                (call, call, int(keep)))
            self._db.commit()
        return {"id": bid, "created": iso_of(stamp), "label": (label or "")[:48],
                "size": len(text)}

    def sync_backup_list(self, call):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, created, label, size FROM user_backups WHERE call = ? "
                "ORDER BY rowid DESC", (call,)).fetchall()
        return [{"id": row["id"],
                 "created": iso_of(row["created"]) if row["created"] else None,
                 "label": row["label"] or "", "size": row["size"] or 0}
                for row in rows]

    def sync_backup_get(self, call, bid):
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM user_backups WHERE call = ? AND id = ?",
                (call, bid)).fetchone()
        if not row or not row["payload"]:
            return None
        try:
            return gzip.decompress(row["payload"]).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def sync_backup_delete(self, call, bid):
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM user_backups WHERE call = ? AND id = ?", (call, bid))
            self._db.commit()
        return cursor.rowcount

    def sync_overview(self):
        """控制台用：每个呼号三类数据各有多少。"""
        calls = set()
        with self._lock:
            for table in ("sync_devices", "user_messages", "user_tracks",
                          "user_backups"):
                for row in self._db.execute("SELECT DISTINCT call FROM %s" % table):
                    calls.add(row["call"])
        rows = []
        for call in sorted(calls):
            days = self.sync_track_days(call)
            rows.append({
                "call": call,
                "devices": len(self.sync_devices(call)),
                "messages": self.sync_messages_meta(call)["count"],
                "track_days": len(days),
                "track_points": sum(day["points"] for day in days),
                "backups": len(self.sync_backup_list(call)),
            })
        return rows

    def sync_clear(self, call):
        """本人一键清空：消息、轨迹、备份一起走。

        令牌与设备行不动 —— 清数据不该把人踢下线；要停用某台设备是另一个接口。
        """
        kept = 0
        with self._lock:
            for table in ("user_messages", "user_tracks", "user_backups"):
                cursor = self._db.execute("DELETE FROM %s WHERE call = ?" % table, (call,))
                kept += cursor.rowcount
            self._db.commit()
        return kept

    # ── 审计与元信息 ──
    def audit(self, kind, detail=None):
        with self._lock:
            self._db.execute("INSERT INTO audit (ts, kind, detail) VALUES (?,?,?)",
                             (int(datetime.now(timezone.utc).timestamp()), kind,
                              json.dumps(detail or {}, ensure_ascii=False)))
            self._db.commit()

    def audits(self, limit=200, kind=None, q=None, before=None):
        """审计行，新的在前。kind 是精确匹配，q 在 kind 与 detail 里找子串，
        before 是游标（上一页最后一行的 id），用来往后翻。"""
        where, args = self._audit_where(kind, q)
        if before:
            where.append("id < ?")
            args.append(int(before))
        sql = "SELECT * FROM audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                detail = {}
            out.append({"id": row["id"], "time": iso_of(row["ts"]),
                        "kind": row["kind"], "detail": detail})
        return out

    def _audit_where(self, kind=None, q=None):
        where, args = [], []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if q:
            like = "%" + q + "%"
            where.append("(kind LIKE ? OR detail LIKE ?)")
            args.extend([like, like])
        return where, args

    def audit_count(self, kind=None, q=None):
        """同一个筛条件的总行数，跟 audits() 的 where 用同一套写法。"""
        where, args = self._audit_where(kind, q)
        sql = "SELECT COUNT(*) AS n FROM audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return row["n"] if row else 0

    def audit_kinds(self):
        """库里出现过的审计类型与条数，给控制台做筛选下拉框。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) AS n FROM audit GROUP BY kind "
                "ORDER BY n DESC, kind").fetchall()
        return [{"kind": row["kind"], "count": row["n"]} for row in rows]

    def meta(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?",
                                   (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()


def _pack_points(points):
    """轨迹点落库：紧凑 JSON + gzip。一天几千个点，原样存占地方。"""
    raw = json.dumps(points, ensure_ascii=False, separators=(",", ":"))
    return gzip.compress(raw.encode("utf-8"))


# 多台设备合并的口径：时间差 3 秒、距离 50 米以内的两个点当成同一个点。
# 两台手机都开着记录时，同一趟路会各记一条；一个点都不能少，但也不能一条路上
# 叠两条重复的线。
MERGE_SECONDS = 3
MERGE_METERS = 50.0


def _point_ts(point):
    try:
        return int(point.get("t") or 0)
    except (TypeError, ValueError):
        return 0


def _meters(a, b):
    """两个点之间的球面距离（米）。"""
    try:
        lat1, lon1 = float(a.get("lat")), float(a.get("lng"))
        lat2, lon2 = float(b.get("lat")), float(b.get("lng"))
    except (TypeError, ValueError):
        return None
    rad = 0.017453292519943295
    dlat = (lat2 - lat1) * rad
    dlon = (lon2 - lon1) * rad
    value = (0.5 - math.cos(dlat) / 2
             + math.cos(lat1 * rad) * math.cos(lat2 * rad)
             * (1 - math.cos(dlon)) / 2)
    return 12742000 * math.asin(math.sqrt(max(0.0, min(1.0, value))))


def _same_spot(left, right):
    if abs(_point_ts(left) - _point_ts(right)) > MERGE_SECONDS * 1000:
        return False
    gap = _meters(left, right)
    return gap is not None and gap <= MERGE_METERS


def _richer(new, keep):
    """两条描述同一位置时留哪条：字段多的，其次是带心率的。"""
    if len(new) != len(keep):
        return len(new) > len(keep)
    return "hr" in new and "hr" not in keep


def merge_points(blobs):
    """把多台设备的轨迹并成一条：按时间排，隔得近的重复点归并成一个。"""
    points = []
    for blob in blobs:
        points.extend(_unpack_points(blob))
    points.sort(key=_point_ts)
    merged = []
    for point in points:
        if merged and _same_spot(merged[-1], point):
            if _richer(point, merged[-1]):
                merged[-1] = point
            continue
        merged.append(point)
    return merged


def _unpack_points(blob):
    """读回来；坏了就当这一天没有，不能拖垮别人的同步。"""
    if not blob:
        return []
    try:
        data = json.loads(gzip.decompress(blob).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    return [point for point in data if isinstance(point, dict)] if isinstance(data, list) else []


def _station_dict(call, row, packets):
    return {
        "call": call,
        "last_seen": iso_of(row["ts"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "symbol": {"table": row["sym_table"], "code": row["sym_code"]},
        "speed": row["speed"],
        "course": row["course"],
        "alt": row["alt"],
        "comment": row["comment"] or "",
        "to_call": (row["dst"] if "dst" in row.keys() else "") or "",
        "via": row["via"] or "is",
        "packets": packets,
    }
