"""账号与名单：口令哈希、状态判定、名单同步的签名校验。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

PBKDF2_ROUNDS = 120000
STATUSES = ("pending", "authorized", "expired", "suspended", "revoked")
OPEN_STATUSES = ("pending", "authorized")   # 会按有效期自动算过期


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), rounds).hex()
    return "pbkdf2$%d$%s$%s" % (rounds, salt, digest)


def verify_password(password, stored):
    if not stored or not password:
        return False
    try:
        scheme, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    try:
        want = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   salt.encode("utf-8"), int(rounds)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(want, digest)


def effective_status(status, valid_from, valid_to, now):
    """算出生效中的状态：名单里写的时间到了就自动过期。"""
    status = (status or "pending").lower()
    if status == "authorized":
        if valid_from and valid_from > now:
            return "pending"
        if valid_to and valid_to < now:
            return "expired"
        return "authorized"
    return status


def may_login(record, now):
    """这条名单记录能不能放行新连接。返回 (允许, 原因)。"""
    if record is None:
        return False, "not-in-whitelist"
    status = effective_status(record.get("status"), record.get("valid_from"),
                              record.get("valid_to"), now)
    if status != "authorized":
        return False, status
    return True, "ok"


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign(payload, secret):
    return hmac.new(secret.encode("utf-8"), canonical(payload),
                    hashlib.sha256).hexdigest()


def verify_signature(payload, signature, secret):
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(payload, secret), str(signature).lower())


def new_token():
    return secrets.token_urlsafe(24)


def constant_equal(left, right):
    return hmac.compare_digest(str(left or ""), str(right or ""))


def file_secret(path, length=24):
    """管理口令之类的临时值，落到文件里，权限 600。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(length)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value
