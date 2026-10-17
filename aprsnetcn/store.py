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
        via TEXT,
        src_call TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts)",
    "CREATE INDEX IF NOT EXISTS positions_call_ts ON positions(call, ts)",
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
            self._db.commit()

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
                       speed, course, alt, comment, via, src_call)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pkt.src, stamp, pkt.lat, pkt.lon, pkt.sym_table, pkt.sym_code,
                 pkt.speed, pkt.course, pkt.alt, (pkt.comment or "")[:160], via,
                 src_call))
            self._db.commit()
        return True

    def purge(self, hours=24, now=None):
        stamp = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        edge = stamp - int(hours * 3600)
        with self._lock:
            cursor = self._db.execute("DELETE FROM positions WHERE ts < ?", (edge,))
            self._db.commit()
        return cursor.rowcount

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
        sql.append("GROUP BY call ORDER BY last_ts DESC LIMIT ? OFFSET ?")
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

    def audits(self, limit=200):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                detail = {}
            out.append({"id": row["id"], "time": iso_of(row["ts"]),
                        "kind": row["kind"], "detail": detail})
        return out

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
        "via": row["via"] or "is",
        "packets": packets,
    }
