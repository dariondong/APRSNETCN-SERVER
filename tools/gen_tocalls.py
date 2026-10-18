#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 TOCALL 查表生成为 Python 模块（`aprsnetcn/tocalls.py`）。

数据来源：https://github.com/aprsorg/aprs-deviceid （tocalls.yaml，CC BY-SA 2.0）。
CN 站已有生成好的 `tocalls.js`（同一个源），这里直接读它，省一份 YAML 解析与
一份 PyYAML 依赖：

    python3 tools/gen_tocalls.py ../APRSNET.CN/tocalls.js

键里带 `?` 的表示该位可变（按 [0-9A-Z] 匹配），带 `*` 的是前缀。
每个目的呼号带两样东西：型号（`APRSdroid Open Source`）与类别（`手机应用`）。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "aprsnetcn", "tocalls.py")

# 本项目自己的目的呼号（官方表里没有）。生成时原样带进 tocalls.py，
# 别改生成出来的文件 —— 改了下次重生成就没了。
LOCALS = {
    "APALOC": ("APRSLocus", "手机应用"),
}

# 上游表里漏译 / 放错位置的类别
CLASS_FIX = {"embedded": "嵌入式", "Windows": "桌面软件"}

ENTRY = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')

HEAD = '''"""TOCALL（目的呼号）→ 设备型号与类别（自动生成，勿手改）。

来源：https://github.com/aprsorg/aprs-deviceid （tocalls.yaml，CC BY-SA 2.0）
重生成：python3 tools/gen_tocalls.py <CN 站的 tocalls.js>

CN 站的值本来是「型号 · 类别 · 系统」，这里拆成两样：型号进 TOCALLS，
类别进 CLASSES（排行按类别归类用）。带 `?` 的键该位可变，带 `*` 的是前缀。
共 %d 条，另有本地补的 %d 条。
"""

'''


def _lit(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def main(src, dest=DEST):
    text = io.open(src, encoding="utf-8").read()
    names = []
    classes = []
    seen = set()
    for key, value in ENTRY.findall(text):
        if key in seen:
            continue
        seen.add(key)
        parts = [piece.strip() for piece in value.split(" · ")]
        model = parts[0] if parts else value.strip()
        cls = parts[1] if len(parts) > 1 else ""
        cls = CLASS_FIX.get(cls, cls)
        names.append('    "%s": "%s",' % (_lit(key), _lit(model)))
        classes.append('    "%s": "%s",' % (_lit(key), _lit(cls)))
    body = HEAD % (len(names), len(LOCALS))
    body += "TOCALLS = {\n" + "\n".join(names) + "\n}\n\n"
    body += "CLASSES = {\n" + "\n".join(classes) + "\n}\n\n"
    body += "LOCALS = {\n"
    for key, (model, cls) in LOCALS.items():
        body += '    "%s": ("%s", "%s"),\n' % (_lit(key), _lit(model), _lit(cls))
    body += "}\n\n"
    body += '''
_EXACT = {}
for _key, (_model, _cls) in LOCALS.items():
    _EXACT[_key] = (_model, _cls)
for _key, _model in TOCALLS.items():
    if "?" not in _key and not _key.endswith("*"):
        _EXACT[_key] = (_model, CLASSES.get(_key, ""))
_PATTERNS = [(_key, _name) for _key, _name in TOCALLS.items()
             if "?" in _key and not _key.endswith("*")]
_PREFIXES = [(_key[:-1], _name) for _key, _name in TOCALLS.items()
             if _key.endswith("*")]


def find(dst):
    """目的呼号 → (型号, 类别)；认不出返回 ("", "")。"""
    dst = (dst or "").strip().upper()
    if not dst:
        return "", ""
    hit = _EXACT.get(dst)
    if hit:
        return hit
    for pattern, name in _PATTERNS:
        if len(pattern) != len(dst):
            continue
        if all(p == "?" or p == ch for p, ch in zip(pattern, dst)):
            return name, CLASSES.get(pattern, "")
    for prefix, name in _PREFIXES:
        if dst.startswith(prefix):
            return name, CLASSES.get(prefix + "*", "")
    return "", ""


def model_of(dst):
    return find(dst)[0]


def class_of(dst):
    return find(dst)[1]
'''
    io.open(dest, "w", encoding="utf-8").write(body)
    print("wrote %s：%d 条，%d 字节" % (dest, len(names), len(body)))


if __name__ == "__main__":
    main(sys.argv[1])
