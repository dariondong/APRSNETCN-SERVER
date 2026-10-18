#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 TOCALL 查表生成为 Python 模块（`aprsnetcn/tocalls.py`）。

数据来源：https://github.com/aprsorg/aprs-deviceid （tocalls.yaml，CC BY-SA 2.0）。
CN 站已有生成好的 `tocalls.js`（同一个源），这里直接读它，省一份 YAML 解析与
一份 PyYAML 依赖：

    python3 tools/gen_tocalls.py ../APRSNET.CN/tocalls.js

键里带 `?` 的表示该位可变（按 [0-9A-Z] 匹配），带 `*` 的是前缀。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "aprsnetcn", "tocalls.py")

ENTRY = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')
HEAD = '''"""TOCALL（目的呼号）→ 设备名查表（自动生成，勿手改）。

来源：https://github.com/aprsorg/aprs-deviceid （tocalls.yaml，CC BY-SA 2.0）
重生成：python3 tools/gen_tocalls.py <CN 站的 tocalls.js>

值的写法与 CN 站一致：本来是「型号 · 类别 · 系统」，这里只留型号（类别与
系统对排行没有用）。带 `?` 的键该位可变，带 `*` 的是前缀。
共 %d 条。
"""

# 本项目自己的目的呼号（官方表里没有）。生成时会原样带进 tocalls.py，
# 别改生成出来的文件 —— 改了下次重生成就没了。
LOCALS = {
    "APALOC": "APRSLocus",
}

TOCALLS = {
'''


def main(src, dest=DEST):
    text = io.open(src, encoding="utf-8").read()
    pairs = []
    seen = set()
    for key, value in ENTRY.findall(text):
        if key in seen:
            continue
        seen.add(key)
        model = value.split(" · ")[0].strip()
        model = model.replace("\\", "\\\\").replace('"', '\\"')
        key_out = key.replace("\\", "\\\\").replace('"', '\\"')
        pairs.append('    "%s": "%s",' % (key_out, model))
    body = HEAD % len(pairs) + "\n".join(pairs) + '''
}

_EXACT = dict(LOCALS)
_EXACT.update({key: name for key, name in TOCALLS.items()
          if "?" not in key and not key.endswith("*")})
_PATTERNS = [(key, name) for key, name in TOCALLS.items()
             if "?" in key and not key.endswith("*")]
_PREFIXES = [(key[:-1], name) for key, name in TOCALLS.items()
             if key.endswith("*")]


def model_of(dst):
    """目的呼号 → 设备名；认不出返回空串（调用方自己决定怎么标）。"""
    dst = (dst or "").strip().upper()
    if not dst:
        return ""
    name = _EXACT.get(dst)
    if name:
        return name
    for pattern, name in _PATTERNS:
        if len(pattern) != len(dst):
            continue
        if all(p == "?" or p == ch for p, ch in zip(pattern, dst)):
            return name
    for prefix, name in _PREFIXES:
        if dst.startswith(prefix):
            return name
    return ""
'''
    io.open(dest, "w", encoding="utf-8").write(body)
    print("wrote %s：%d 条，%d 字节" % (dest, len(pairs), len(body)))


if __name__ == "__main__":
    main(sys.argv[1])
