"""SQLite 存储：账号与名单、24 小时位置、审计与元信息。"""

from __future__ import annotations

import json
import os
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
