#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Natural Earth 110m 陆地多边形压成一张很小的世界轮廓，给台站页画图用。

    python3 tools/gen_coast.py ne_110m_land.geojson aprsnetcn/coast.json

数据源 https://github.com/nvkelso/natural-earth-vector （Natural Earth，公有领域）。

只保留面积够大的环，再把每环按 Douglas-Peucker 抽稀到 0.3 度以内，坐标留两位小数。
出来的文件大约几十 KB，浏览器直接画，不连任何地图瓦片服务器 —— 台站页在
国内网络下也不该因为一张底图转圈。
"""
import json
import sys

TOLERANCE = 0.30      # 度。0.3 度在 110m 数据上已经看不出删点
MIN_POINTS = 6
MIN_SPAN = 1.0        # 环的经纬跨度小于 1 度就当小岛扔掉


def _perp(point, start, end):
    (x, y), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px, py = x1 + t * dx, y1 + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def simplify(points, tolerance=TOLERANCE):
    """Douglas-Peucker，迭代写法，避免深递归。"""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        top = 0.0
        index = -1
        for i in range(first + 1, last):
            d = _perp(points[i], points[first], points[last])
            if d > top:
                top, index = d, i
        if index >= 0 and top > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [points[i] for i in range(len(points)) if keep[i]]


def rings_of(geometry):
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        for ring in coords:
            yield ring
    elif kind == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                yield ring


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    out = []
    features = data.get("features") or []
    for feature in features:
        for ring in rings_of(feature.get("geometry") or {}):
            if len(ring) < MIN_POINTS:
                continue
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            if max(lons) - min(lons) < MIN_SPAN and max(lats) - min(lats) < MIN_SPAN:
                continue
            thin = simplify([(float(p[0]), float(p[1])) for p in ring])
            if len(thin) < 4:
                continue
            flat = []
            for lon, lat in thin:
                flat.append(round(lon, 2))
                flat.append(round(lat, 2))
            out.append(flat)

    payload = {"source": "Natural Earth 110m land (public domain)",
               "tolerance": TOLERANCE, "rings": out}
    with open(dst, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    points = sum(len(r) // 2 for r in out)
    print("%s: %d rings, %d points" % (dst, len(out), points))
    return 0


if __name__ == "__main__":
    sys.exit(main())
