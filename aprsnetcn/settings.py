"""控制台里能直接改的运行参数。

env 只给启动时一个默认值：控制台改过的值存进库里（meta 表的 setting.* 键），
下次启动先读库再盖进 config。调用处都是现读 config.XXX，所以改完不重启也生效；
端口、数据目录这类必须重启的留在 env 里，这里只做只读展示。

改一项的顺序是：先全部校验，全部通过才写库、才改内存、才记审计，
免得半截改成功、一半留在老值上。
"""

from __future__ import annotations

from . import config

PREFIX = "setting."

# 每组一段，控制台按这个顺序排
GROUPS = ("接入", "数据", "外部拉取", "控制台")

SPECS = (
    # ── 接入 ──
    {"key": "ACN_PASSCODE_MODE", "attr": "PASSCODE_MODE", "group": "接入",
     "label": "passcode 校验", "kind": "enum", "options": ("compat", "strict", "off"),
     "hint": "compat：passcode 不对只给收（和 APRS-IS 一样）；strict：不对直接拒；off：不校验"},
    {"key": "ACN_AUTH_MODE", "attr": "AUTH_MODE", "group": "接入",
     "label": "名单口令", "kind": "enum", "options": ("optional", "required"),
     "hint": "optional：没带 auth 就按 passcode 放行，标准客户端能直接用；required：必须带 auth"},
    {"key": "ACN_SRC_POLICY", "attr": "SRC_POLICY", "group": "接入",
     "label": "源呼号不一致", "kind": "enum", "options": ("allow", "warn", "reject"),
     "hint": "报文里的呼号和登录呼号不一样时：放行 / 放行并记一条 / 拒收"},
    {"key": "ACN_WHITELIST_MAX_AGE_HOURS", "attr": "WHITELIST_MAX_AGE_HOURS",
     "group": "接入", "label": "名单同步时效", "kind": "int", "min": 0, "max": 8760,
     "hint": "超过这么多小时没同步过名单就不再放行新连接；0 不限制"},
    {"key": "ACN_MAX_CONN", "attr": "MAX_CONN", "group": "接入",
     "label": "同时在线上限", "kind": "int", "min": 1, "max": 20000},
    {"key": "ACN_RATE_LINES", "attr": "RATE_LINES", "group": "接入",
     "label": "每连接限流条数", "kind": "int", "min": 1, "max": 100000,
     "hint": "每个连接在一个限流窗口里最多收这么多行"},
    {"key": "ACN_RATE_WINDOW", "attr": "RATE_WINDOW", "group": "接入",
     "label": "限流窗口", "kind": "int", "min": 1, "max": 3600, "unit": "秒"},
    {"key": "ACN_ADD_Q_CONSTRUCT", "attr": "ADD_Q_CONSTRUCT", "group": "接入",
     "label": "补 q 构造", "kind": "bool",
     "hint": "客户端报文转发时补上 qAC,登录呼号，与 APRS-IS 的行为一致"},
    # ── 数据 ──
    {"key": "ACN_RETENTION_HOURS", "attr": "RETENTION_HOURS", "group": "数据",
     "label": "台站保留", "kind": "int", "min": 1, "max": 720, "unit": "小时"},
    {"key": "ACN_RECENT_LINES", "attr": "RECENT_LINES", "group": "数据",
     "label": "报文环", "kind": "int", "min": 50, "max": 20000, "unit": "行",
     "hint": "控制台「报文」页读的内存环，只留最近这么多行，重启就空"},
    {"key": "ACN_ARCHIVE_ENABLED", "attr": "ARCHIVE_ENABLED", "group": "数据",
     "label": "报文存档", "kind": "bool",
     "hint": "打开后整行报文进库，按下面的小时数删；草案 5.5 默认关"},
    {"key": "ACN_ARCHIVE_HOURS", "attr": "ARCHIVE_HOURS", "group": "数据",
     "label": "存档保留", "kind": "int", "min": 1, "max": 2160, "unit": "小时"},
    {"key": "ACN_PUBLIC_API", "attr": "PUBLIC_API", "group": "数据",
     "label": "台站数据对外", "kind": "bool",
     "hint": "打开后 /stations 与 /api/public/* 不验口令也能看"},
    {"key": "ACN_PUBLIC_RATE_MINUTE", "attr": "PUBLIC_RATE_MINUTE", "group": "数据",
     "label": "公开接口限流", "kind": "int", "min": 1, "max": 100000, "unit": "次/分",
     "hint": "按来源 IP 数，限的是刷新太勤，不是防人"},
    # ── 外部拉取 ──
    {"key": "ACN_INGEST_ENABLED", "attr": "INGEST_ENABLED", "group": "外部拉取",
     "label": "拉取外部数据", "kind": "bool",
     "hint": "只读地连一台 APRS-IS，把报文补进 24 小时台站；改完立刻重连"},
    {"key": "ACN_INGEST_HOST", "attr": "INGEST_HOST", "group": "外部拉取",
     "label": "上游地址", "kind": "text", "maxlen": 120},
    {"key": "ACN_INGEST_PORT", "attr": "INGEST_PORT", "group": "外部拉取",
     "label": "上游端口", "kind": "int", "min": 1, "max": 65535},
    {"key": "ACN_INGEST_LOGIN", "attr": "INGEST_LOGIN", "group": "外部拉取",
     "label": "登录呼号", "kind": "text", "maxlen": 16, "pattern": r"^[A-Za-z0-9-]*$",
     "hint": "拉取用 pass -1 只收，这个呼号只出现在上游的登录行里"},
    {"key": "ACN_INGEST_FILTER", "attr": "INGEST_FILTER", "group": "外部拉取",
     "label": "上游过滤器", "kind": "text", "maxlen": 200,
     "hint": "例如 r/35/105/2000 是以 35N 105E 为心、2000 km 范围的圆"},
    {"key": "ACN_INGEST_FORWARD", "attr": "INGEST_FORWARD", "group": "外部拉取",
     "label": "转给客户端", "kind": "bool",
     "hint": "默认关：外部报文只进 24 小时库，不转发，免得把别人的流量灌进来"},
    # ── 控制台 ──
    {"key": "ACN_ADMIN_PASSWORD", "attr": "ADMIN_PASSWORD", "group": "控制台",
     "label": "管理口令", "kind": "secret", "minlen": 4, "maxlen": 64,
     "hint": "改完这一页会自动换成新口令，旧口令立即失效；留空表示不改"},
    {"key": "ACN_TOKEN_TTL", "attr": "TOKEN_TTL", "group": "控制台",
     "label": "接口令牌有效期", "kind": "int", "min": 60, "max": 86400, "unit": "秒"},
    {"key": "ACN_API_RATE_MINUTE", "attr": "API_RATE_MINUTE", "group": "控制台",
     "label": "接口限流", "kind": "int", "min": 1, "max": 100000, "unit": "次/分"},
)

# env 里的默认值，用来「恢复默认」和标出来源
ENV_VALUES = {spec["attr"]: getattr(config, spec["attr"]) for spec in SPECS}

# 必须改 env 重启才生效的项，控制台只读展示
RESTART_ONLY = (
    ("ACN_TCP_HOST", "TCP_HOST", "监听地址"),
    ("ACN_TCP_PORT", "TCP_PORT", "客户端端口"),
    ("ACN_TCP_PORT_TLS", "TCP_PORT_TLS", "TLS 端口（0 不开）"),
    ("ACN_HTTP_HOST", "HTTP_HOST", "HTTP 监听地址"),
    ("ACN_HTTP_PORT", "HTTP_PORT", "HTTP 端口"),
    ("ACN_DATA_DIR", "DATA_DIR", "数据目录"),
)

_BY_KEY = {spec["key"]: spec for spec in SPECS}
_BY_ATTR = {spec["attr"]: spec for spec in SPECS}


def spec_of(key):
    return _BY_KEY.get(key)


def _to_text(spec, value):
    if spec["kind"] == "bool":
        return "1" if value else "0"
    if spec["kind"] in ("int",):
        return str(int(value))
    return str(value)


def parse(spec, raw):
    """把界面上交的原始值变成 config 里的值。返回 (value, error)。"""
    if isinstance(raw, bool):
        raw = "1" if raw else "0"
    text = "" if raw is None else str(raw).strip()
    kind = spec["kind"]
    if kind == "bool":
        low = text.lower()
        if low in ("1", "true", "on", "yes"):
            return True, ""
        if low in ("0", "false", "off", "no", ""):
            return False, ""
        return None, "只能是开或关"
    if kind == "int":
        if not text:
            return None, "要填一个整数"
        try:
            number = int(text)
        except ValueError:
            return None, "要填一个整数"
        low = spec.get("min")
        high = spec.get("max")
        if low is not None and number < low:
            return None, "不能小于 %s" % low
        if high is not None and number > high:
            return None, "不能大于 %s" % high
        return number, ""
    if kind == "enum":
        if text.lower() not in spec["options"]:
            return None, "只能是 " + " / ".join(spec["options"])
        return text.lower(), ""
    if kind == "secret":
        if not text:
            return "", ""                                  # 空 = 不改
        if len(text) < spec.get("minlen", 1):
            return None, "至少 %s 个字符" % spec.get("minlen", 1)
        if len(text) > spec.get("maxlen", 200):
            return None, "太长了"
        return text, ""
    # text
    if len(text) > spec.get("maxlen", 200):
        return None, "太长了"
    pattern = spec.get("pattern")
    if pattern:
        import re
        if text and not re.match(pattern, text):
            return None, "字符不合适"
    return text, ""


def _value_of(spec):
    """当前生效的值（给接口看）。口令不回显。"""
    current = getattr(config, spec["attr"])
    if spec["kind"] == "secret":
        return "" if not current else "已设置"
    return current


def rows(store):
    """设置页要的全部行，带上来源与当前值。"""
    out = []
    for spec in SPECS:
        stored = store.meta(PREFIX + spec["key"])
        row = {
            "key": spec["key"],
            "group": spec["group"],
            "label": spec["label"],
            "kind": spec["kind"],
            "hint": spec.get("hint", ""),
            "value": _value_of(spec),
            "source": "db" if stored is not None else "env",
            "env": ("" if spec["kind"] == "secret"
                    else _to_text(spec, ENV_VALUES[spec["attr"]])),
        }
        for field in ("options", "min", "max", "unit", "minlen", "maxlen"):
            if field in spec:
                row[field] = spec[field]
        out.append(row)
    return out


def restart_rows():
    return [{"key": key, "label": label, "value": getattr(config, attr)}
            for key, attr, label in RESTART_ONLY]


def load(store):
    """启动时把库里存过的值盖回 config。坏值跳过并记一条审计。"""
    applied = []
    for spec in SPECS:
        raw = store.meta(PREFIX + spec["key"])
        if raw is None:
            continue
        value, error = parse(spec, raw)
        if error or (spec["kind"] == "secret" and not value):
            store.audit("settings-skip", {"key": spec["key"], "value": raw[:40],
                                          "error": error or "empty"})
            continue
        setattr(config, spec["attr"], value)
        applied.append(spec["key"])
    return applied


def save(store, runtime, updates):
    """一次改多项。先全部校验，再写库、改内存、按需要重启拉取线程。"""
    if not isinstance(updates, dict) or not updates:
        return {"ok": False, "error": "empty"}
    prepared = []
    errors = {}
    for key, raw in updates.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            errors[key] = "没有这一项"
            continue
        value, error = parse(spec, raw)
        if error:
            errors[key] = error
            continue
        if spec["kind"] == "secret" and not value:
            continue                                       # 留空 = 不改
        prepared.append((spec, value))
    if errors:
        return {"ok": False, "error": "invalid", "fields": errors}

    changed = []
    for spec, value in prepared:
        before = getattr(config, spec["attr"])
        if before == value:
            continue
        setattr(config, spec["attr"], value)
        store.set_meta(PREFIX + spec["key"], _to_text(spec, value))
        changed.append({"key": spec["key"], "label": spec["label"],
                        "before": ("" if spec["kind"] == "secret"
                                   else _to_text(spec, before)),
                        "after": ("" if spec["kind"] == "secret"
                                  else _to_text(spec, value))})
        if spec["kind"] == "secret":
            changed[-1]["secret"] = True

    restart_needed = [row["key"] for row in changed
                      if row["key"].startswith("ACN_INGEST_")]
    if restart_needed and runtime is not None:
        feeder = getattr(runtime, "ingest", None)
        if feeder is not None:
            feeder.restart()
    if any(row["key"] == "ACN_RECENT_LINES" for row in changed) and runtime is not None:
        node = getattr(runtime, "hub", None)
        if node is not None and hasattr(node, "resize_recent"):
            node.resize_recent(config.RECENT_LINES)

    if changed:
        store.audit("settings-changed", {
            "keys": [row["key"] for row in changed],
            "values": {row["key"]: row["after"] for row in changed
                       if not row.get("secret")},
        })
    return {"ok": True, "changed": changed, "changed_keys": [r["key"] for r in changed],
            "ingest_restarted": bool(restart_needed),
            "admin_password_changed": any(row.get("secret") for row in changed)}


def reset(store, runtime, keys=None):
    """把某一项（或全部）退回 env 里的值。口令不参与，退回去等于关掉控制台。"""
    targets = keys or [spec["key"] for spec in SPECS]
    updates = {}
    for key in targets:
        spec = _BY_KEY.get(key)
        if spec is None or spec["kind"] == "secret":
            continue
        updates[key] = _to_text(spec, ENV_VALUES[spec["attr"]])
    if not updates:
        return {"ok": True, "changed": [], "changed_keys": []}
    return save(store, runtime, updates)
