"""APRS 报文的最小解析：拆头、取位置与符号、判类型。

这里只做转发和过滤需要的那部分，不求覆盖全部 APRS 格式。
解析失败不抛异常，返回 None 或一个字段为空的对象。
"""

from __future__ import annotations

import math
import re

CALL_RE = re.compile(r"^[A-Za-z0-9]{1,9}(?:-\d{1,2})?$")
# 报文类型（t/ 过滤器里的那一个字母）
TYPE_OF_DTI = {
    "!": "p", "=": "p", "/": "p", "@": "p",
    ";": "o", ")": "i", ":": "m", "?": "q",
    ">": "s", "T": "t", "{": "u", "_": "w",
    "<": "c", "&": "c", "}": "x", "#": "r", "*": "r", "$": "r",
}
PRINTABLE = set(chr(c) for c in range(0x20, 0x7F))


def valid_call(text):
    return bool(text) and bool(CALL_RE.match(text))


class Packet:
    """一条报文拆出来的东西。所有字段都可能为空。"""

    __slots__ = ("raw", "src", "dst", "path", "info", "type", "lat", "lon",
                 "sym_table", "sym_code", "speed", "course", "alt", "comment")

    def __init__(self):
        self.raw = ""
        self.src = ""
        self.dst = ""
        self.path = []
        self.info = ""
        self.type = ""
        self.lat = None
        self.lon = None
        self.sym_table = ""
        self.sym_code = ""
        self.speed = None
        self.course = None
        self.alt = None
        self.comment = ""

    def __repr__(self):
        return "<Packet %s>%s %s>" % (self.src, self.dst, self.type)


def clean_line(line, max_len):
    """客户端来的一行：去掉行尾、查长度、查可打印字符。

    返回 (line, None) 或 (None, 原因)。
    """
    if line is None:
        return None, "empty"
    line = line.rstrip("\r\n")
    if not line:
        return None, "empty"
    if len(line) > max_len:
        return None, "too-long"
    for ch in line:
        if ch not in PRINTABLE:
            return None, "bad-char"
    return line, None


def split_header(line):
    """`SRC>DST,PATH:info` → (src, dst, path, info)，拆不开返回 None。"""
    if ">" not in line:
        return None
    head, sep, info = line.partition(":")
    if not sep:
        return None
    src, _, rest = head.partition(">")
    src = src.strip().upper()
    if not valid_call(src):
        return None
    items = [piece.strip() for piece in rest.split(",") if piece.strip()]
    if not items:
        return None
    dst = items[0].upper()
    path = [piece.upper() for piece in items[1:]]
    return src, dst, path, info


def third_party(info):
    """第三方报文 `}`：里面套的才是真报文。"""
    if info.startswith("}") and len(info) > 1:
        inner = info[1:]
        if ">" in inner and ":" in inner:
            return inner
    return None


def parse(line):
    """整行 → Packet；不是合法报文返回 None。"""
    pkt = Packet()
    pkt.raw = line
    parts = split_header(line)
    if parts is None:
        return None
    pkt.src, pkt.dst, pkt.path, pkt.info = parts
    info = pkt.info
    inner = third_party(info)
    if inner is not None:
        sub = split_header(inner)
        if sub is None:
            pkt.type = "x"
            return pkt
        pkt.src, pkt.dst, pkt.path, info = sub
        pkt.info = inner
    if not info:
        return None
    pkt.type = TYPE_OF_DTI.get(info[0], "")
    if not pkt.type:
        return None
    if pkt.type in ("p", "o", "i"):
        body = info[1:]
        if pkt.type == "o":
            body = body[9:] if len(body) > 9 else ""      # 对象名占 9 位
        elif pkt.type == "i":
            cut = body.find("_")
            body = body[cut + 1:] if cut >= 0 else (body[4:] if len(body) > 4 else "")
        _read_position(pkt, body)
    elif pkt.type == "w":                                  # 气象报文常常带位置
        _read_position(pkt, info[1:])
    return pkt


def _read_position(pkt, body):
    if not body:
        return
    stamp = 0
    if body[:1] in "/@" and len(body) > 8 and body[1:8].isdigit():
        stamp = 7                                          # 带时间戳的位置
    body = body[stamp:]
    if not body:
        return
    if body[0].isdigit() or body[0] in "/\\":
        _uncompressed(pkt, body)
    else:
        _compressed(pkt, body)


def _uncompressed(pkt, body):
    match = re.match(r"^(\d{2})(\d{2}\.\d{2,3})([NS])(.)(\d{3})(\d{2}\.\d{2,3})([EW])(.)",
                     body)
    if not match:
        return
    lat = int(match.group(1)) + float(match.group(2)) / 60.0
    if match.group(3) == "S":
        lat = -lat
    lon = int(match.group(5)) + float(match.group(6)) / 60.0
    if match.group(7) == "W":
        lon = -lon
    pkt.lat, pkt.lon = round(lat, 5), round(lon, 5)
    pkt.sym_table = match.group(4) or "/"
    pkt.sym_code = match.group(8) or "-"
    rest = body[match.end():]
    course = re.match(r"^(\d{3})/(\d{3})", rest)
    if course:
        pkt.course = float(course.group(1))
        pkt.speed = float(course.group(2))
        rest = rest[7:]
    pkt.comment = rest.strip()
    alt = re.search(r"/A=(\d{6})", pkt.comment or "")
    if alt:
        pkt.alt = float(int(alt.group(1)))


def _compressed(pkt, body):
    if len(body) < 10:
        return
    table = body[0]
    compressed = body[1:5]
    lon_part = body[5:9]
    code = body[9]
    comp_type = body[10] if len(body) > 10 else ""
    try:
        y = 0
        for ch in compressed:
            value = ord(ch) - 33
            if value < 0 or value > 90:
                return
            y = y * 91 + value
        x = 0
        for ch in lon_part:
            value = ord(ch) - 33
            if value < 0 or value > 90:
                return
            x = x * 91 + value
    except (TypeError, ValueError):
        return
    pkt.lat = round(90.0 - y / 380926.0, 5)
    pkt.lon = round(-180.0 + x / 190463.0, 5)
    if table.isalpha() or table.isdigit():
        pkt.sym_table = "\\"                               # 叠字 = 备用表
        pkt.sym_code = table
    else:
        pkt.sym_table = table
        pkt.sym_code = code
    pkt.comment = body[11:].strip() if len(body) > 11 else ""
    if comp_type and comp_type.isdigit():
        pass
    alt = re.search(r"/A=(\d{6})", pkt.comment or "")
    if alt:
        pkt.alt = float(int(alt.group(1)))


def distance_km(lat1, lon1, lat2, lon2):
    """两点间球面距离（公里）。"""
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def passcode(call):
    """APRS-IS 的老算法：0x73e2 起步，奇数位左移 8 位再异或，最后 & 0x7fff。"""
    root = call.split("-")[0].upper()
    code = 0x73E2
    for index, ch in enumerate(root):
        if index % 2 == 0:
            code ^= ord(ch) << 8
        else:
            code ^= ord(ch)
    return code & 0x7FFF
