"""APRS-IS 过滤器的一个子集。

只实现服务器真能算的那些前缀；认不出来的直接拒（草案 6.2）。
每条连接的过滤器只影响它自己（草案 6.3）。
"""

from __future__ import annotations

from . import aprslib

MAX_SPECS = 16
MAX_SPEC_LEN = 200

# 认的前缀
KNOWN = "rabmtpdfsuq"


class FilterError(Exception):
    pass


class Context:
    """过滤器匹配时的上下文：这条连接自己的呼号与最近位置。"""

    __slots__ = ("call", "lat", "lon")

    def __init__(self, call="", lat=None, lon=None):
        self.call = call
        self.lat = lat
        self.lon = lon


def _wild(text, target):
    """`BG7LZQ*` 这种简单通配。"""
    if text.endswith("*"):
        return target.startswith(text[:-1])
    return target == text


class Filter:
    def __init__(self):
        self.active = []
        self.predicates = []

    def match(self, pkt, ctx):
        if not self.predicates:
            return True                                    # 没过滤器 = 全收
        for predicate in self.predicates:
            if predicate(pkt, ctx):
                return True
        return False

    # ── 解析 ──
    @staticmethod
    def parse(specs):
        result = Filter()
        if specs is None:
            return result
        if len(specs) > MAX_SPECS:
            raise FilterError("too-many")
        for spec in specs:
            spec = (spec or "").strip()
            if not spec:
                continue
            if len(spec) > MAX_SPEC_LEN:
                raise FilterError("too-long")
            head, _, body = spec.partition("/")
            head = head.lower()
            if head not in KNOWN:
                raise FilterError("unsupported:" + head)
            builder = _BUILDERS.get(head)
            if builder is None:
                raise FilterError("unsupported:" + head)
            result.predicates.append(builder(body, spec))
            result.active.append(spec)
        return result


def _number(text, name):
    try:
        return float(text)
    except (TypeError, ValueError):
        raise FilterError("bad-%s" % name)


def _build_range(body, spec):
    """r/lat/lon/km —— 与 a/ 一样按距离算。"""
    parts = body.split("/")
    if len(parts) != 3:
        raise FilterError("bad-range")
    lat, lon, km = _number(parts[0], "lat"), _number(parts[1], "lon"), _number(parts[2], "km")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < km <= 20000):
        raise FilterError("bad-range")

    def predicate(pkt, ctx):
        return (aprslib.distance_km(lat, lon, pkt.lat, pkt.lon) or 1e9) <= km

    return predicate


def _build_my_range(body, spec):
    """m/km —— 以这条连接自己的位置为圆心。"""
    km = _number(body.split("/")[0], "km")
    if not 0 < km <= 20000:
        raise FilterError("bad-range")

    def predicate(pkt, ctx):
        if ctx.lat is None or ctx.lon is None:
            return False
        return (aprslib.distance_km(ctx.lat, ctx.lon, pkt.lat, pkt.lon) or 1e9) <= km

    return predicate


def _build_calls(body, spec):
    """b/call1/call2 —— 报文源或目的呼号命中。"""
    wanted = [piece for piece in body.split("/") if piece]
    if not wanted:
        raise FilterError("bad-budlist")

    def predicate(pkt, ctx):
        for piece in wanted:
            if _wild(piece.upper(), pkt.src) or _wild(piece.upper(), pkt.dst):
                return True
        return False

    return predicate


def _build_prefix(body, spec):
    """p/aa/bb —— 源呼号前缀。"""
    wanted = [piece.upper() for piece in body.split("/") if piece]
    if not wanted:
        raise FilterError("bad-prefix")

    def predicate(pkt, ctx):
        return any(pkt.src.startswith(piece) for piece in wanted)

    return predicate


def _build_type(body, spec):
    """t/poimqstunw —— 报文类型。"""
    wanted = set(ch.lower() for ch in body if ch.isalpha())
    if not wanted:
        raise FilterError("bad-type")

    def predicate(pkt, ctx):
        return pkt.type in wanted

    return predicate


def _build_digi(body, spec):
    """d/call1/call2 —— 报文用过的中继。"""
    wanted = [piece.upper() for piece in body.split("/") if piece]
    if not wanted:
        raise FilterError("bad-digi")

    def predicate(pkt, ctx):
        for entry in pkt.path:
            if entry.split("*")[0] in ("", "TCPIP", "TCPXX"):
                continue
            if any(_wild(piece, entry) for piece in wanted):
                return True
        return False

    return predicate


def _build_friend(body, spec):
    """f/call/km 或 f/call1/call2 —— 朋友范围。"""
    parts = [piece for piece in body.split("/") if piece]
    if not parts:
        raise FilterError("bad-friend")
    if len(parts) == 1:
        calls, km = [parts[0].upper()], None
    else:
        calls, km = [piece.upper() for piece in parts[:-1]], _number(parts[-1], "km")

    def predicate(pkt, ctx):
        if not any(_wild(piece, pkt.src) for piece in calls):
            return False
        if km is None or ctx.lat is None or ctx.lon is None:
            return km is None
        return (aprslib.distance_km(ctx.lat, ctx.lon, pkt.lat, pkt.lon) or 1e9) <= km

    return predicate


def _build_symbol(body, spec):
    """s/P（主表）/ s/A（备用表）/ s/O（叠字）。"""
    letters = body.upper()
    if not letters:
        raise FilterError("bad-symbol")

    def predicate(pkt, ctx):
        if "P" in letters and pkt.sym_table == "/":
            return True
        if "A" in letters and pkt.sym_table == "\\":
            return True
        if "O" in letters and pkt.sym_table not in ("/", "\\", ""):
            return True
        return False

    return predicate


def _build_q(body, spec):
    """q/qAR/qAC —— 报文里的 q 构造。"""
    wanted = [piece.upper() for piece in body.split("/") if piece]
    if not wanted:
        raise FilterError("bad-q")

    def predicate(pkt, ctx):
        for entry in pkt.path:
            for piece in wanted:
                if entry.startswith(piece):
                    return True
        return False

    return predicate


def _build_unicode(body, spec):
    """u/unproto —— 与 d/ 同义，按路径里的中继算。"""
    return _build_digi(body, spec)


_BUILDERS = {
    "r": _build_range,
    "a": _build_range,
    "m": _build_my_range,
    "b": _build_calls,
    "p": _build_prefix,
    "t": _build_type,
    "d": _build_digi,
    "f": _build_friend,
    "s": _build_symbol,
    "q": _build_q,
    "u": _build_unicode,
}
