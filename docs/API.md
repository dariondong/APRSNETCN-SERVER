# 台站接口

服务器把最近 24 小时收到的位置存下来，用 HTTP JSON 读出去。任何客户端都能调，
不限于 APRSLocus。机器可读的完整描述在 `docs/openapi.json`，也可以在
`GET /api/v1/openapi.json` 取到。

## 鉴权

先换 token。token 默认 12 小时有效，每个 token 每分钟 120 次请求。

```bash
curl -s -X POST http://8.138.199.11:8899/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"bg7lzq","password":"……"}'
```

```json
{"ok": true, "token": "…", "call": "BG7LZQ", "expires": "2026-10-18T12:00:00+08:00"}
```

后面每个请求带 `Authorization: Bearer <token>`。

账号必须是名单里已授权、且在有效期内的记录，并且设了口令。

## 台站列表

```
GET /api/v1/stations
```

| 参数 | 说明 |
| --- | --- |
| `window` | 时间窗口，如 `24h`，最多到服务器保留时长 |
| `since` | 只要这个时刻之后的报文，ISO 8601，例如 `2026-10-17T00:00:00+08:00` |
| `bbox` | `minlat,minlon,maxlat,maxlon` |
| `limit` | 默认 500，最大 5000 |
| `offset` | 分页 |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://8.138.199.11:8899/api/v1/stations?window=24h&bbox=18,73,54,135&limit=200'
```

```json
{
  "window": "24h",
  "updated": "2026-10-18T00:12:33+08:00",
  "count": 1,
  "stations": [
    {
      "call": "BG7LZQ-9",
      "last_seen": "2026-10-18T00:09:41+08:00",
      "lat": 39.9042,
      "lon": 116.4074,
      "symbol": {"table": "/", "code": ">"},
      "speed": 0.0,
      "course": 0.0,
      "alt": null,
      "comment": "test packet",
      "via": "is",
      "packets": 3
    }
  ]
}
```

`via` 是这条台站最近的报文从哪来：`rf` 走射频，`is` 走网络。

## 单台站

```
GET /api/v1/stations/{call}
```

比列表多两个字段：`first_seen` 与 `track`（24 小时内的位置点，按时间升序）。
24 小时内没有这台站就返回 404。

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://8.138.199.11:8899/api/v1/stations/BG7LZQ-9'
```

## 不用鉴权的两个

- `GET /api/v1/health` 运行状态：连接数、收发报文数、24 小时台站数。
- `GET /api/v1/version` 协议版本与服务端实际策略（passcode 模式、保留时长）。

## 错误

| 状态码 | `error` | 含义 |
| --- | --- | --- |
| 400 | `bad-bbox` / `bad-since` / `bad-limit` | 参数写错 |
| 401 | `unauthorized` | 没带 token 或已过期 |
| 401 | `bad-credentials` | 账号或密码不对 |
| 403 | `not-authorized` | 名单里没有，或授权已过期、已暂停、已撤销 |
| 404 | `not-found` | 台站不在 24 小时窗口里 |
| 429 | `rate-limited` | 这个 token 请求太密 |

所有响应带 `Access-Control-Allow-Origin: *`，浏览器里可以直接调。

## 写一个客户端时注意

- 位置点是 24 小时滑窗，不是历史档案，别按长期记录用。
- 台站按呼号聚合，同一呼号的最新一条决定 `lat`/`lon`/`comment`。
- `packets` 是 24 小时内的报文条数，不是全站累计。
- 列表按最后听到时间从新到旧，翻页请用 `offset`，不要指望稳定游标。
