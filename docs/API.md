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
  "total": 2324,
  "offset": 0,
  "limit": 200,
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

`total` 是窗口里的台站总数，`count` 是这一页给了几条：翻页就按 `offset` 加
`count` 往后走，`count` 小于 `limit` 说明到底了。排序按最后收到的时间倒序，
时间相同的按呼号升序，所以翻页不会漏也不会重。

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

## 台站页与公开接口

```
GET /stations
GET /api/public/stations            同一份台站列表，参数与 /api/v1/stations 相同
GET /api/public/stations/{call}     单台站与轨迹
```

台站数据默认不对外。`ACN_PUBLIC_API=0`（默认）时这三条都回 `403 not-public`，
打开之后才服务：`/stations` 是一张世界地图，点一个点看详情与轨迹，不用账号；
两个 `/api/public/*` 不验 token，按来源 IP 限流（默认每分钟 60 次）。
带 token 的 `/api/v1/stations` 不受这个开关影响。

## 不用鉴权的两个

- `GET /api/v1/health` 运行状态：连接数、收发报文数、24 小时台站数、本地名单版本与最近同步时间。
  草案 7.4 要求能监控白名单版本与同步时间，这条接口不需要鉴权就能看。
- `GET /api/v1/version` 协议版本与服务端实际策略：`passcode_mode`、`src_policy`、`auth_mode`、
  `retention_hours`、`whitelist_max_age_hours`、`default_filter`（没带过滤器的连接收全部报文）、
  `public_api`（台站数据是否对外）、`filters`（认得的过滤器前缀）。

## 管理接口

控制台和脚本都用 `X-ACN-Token: <ACN_ADMIN_PASSWORD>` 调这几条。

```
GET  /api/admin/state                   账号、审计、总计数
GET  /api/admin/clients                 在线连接
POST /api/admin/kick                    {"call":"BG7LZQ-2"} 踢掉这个呼号
GET  /api/admin/packets?q=&dir=&limit=  最近的内存环
GET  /api/admin/archive?q=&dir=&limit=  存档，开了才有
GET  /api/admin/stations?window=24h&limit=300&offset=0   24 小时台站列表（带 total）
GET  /api/admin/stations/{call}                 单台站与轨迹
GET  /api/admin/audits?kind=&q=&limit=60&before=<id>   审计，可筛类型、搜关键词、往后翻
GET  /api/admin/whitelist                       整份名单（控制台导出的就是这一份）
GET  /api/admin/settings                        能在控制台里改的运行参数
POST /api/admin/settings                        {"values":{"ACN_RATE_LINES":"45"}} 改一项或几项
POST /api/admin/settings/reset                  {"keys":["ACN_RATE_LINES"]} 退回 env，不写就全退
```

`/api/admin/clients` 一条连接一行：呼号、来源地址、`verified`、是否只收、客户端版本、
连上时间、收与发的条数、被丢掉的积压、登录时生效的过滤器。

管理口令错也会记一条 `admin-denied` 审计，控制台的「管理拒绝」就是这个计数。

`kick` 写全 `BG7LZQ-2` 就只断那一条，只写 `BG7LZQ` 会把它下面所有 SSID 的连接一起断开。
被踢的连接会记一条审计。

报文检索的 `dir` 是 `in` 或 `out`，不填两条都看。`q` 在整行报文、源呼号、收件人里找。
内存环只留最近 `ACN_RECENT_LINES` 行（默认 600），重启就空。

台站数据不对外，控制台看台站靠 `/api/admin/stations`；参数与 `/api/v1/stations` 一样。
返回里带 `total`（窗口内的台站总数）、`offset`、`limit`，控制台拿它做分页。
`/api/admin/state` 里的账号不带口令哈希，只给一个 `has_password` 标志。

审计列表：`kind` 精确匹配，`q` 在 `kind` 与 `detail` 里找子串，`before` 是游标
（上一页最后一行的 `id`）。回包里 `total` 是同一筛选条件下的总行数，`next_before` 给下一次往后翻用，
`kinds` 是库里出现过的类型与条数，控制台的筛选下拉框就是它。
`/api/admin/state` 的 `meta.whitelist` 是名单汇总：版本、最近同步时间、条目数、已设口令的条数、
按生效状态的分布，以及同步时效（`max_age_hours`）。

## 运行参数

env 只给启动时的默认值。控制台「设置」页改的值存进库（`meta` 表的 `setting.<KEY>`），
下次启动先读库再盖进 config；能立刻生效的当场就改内存里的 `config.XXX`，
`ACN_INGEST_*` 改了会让拉取线程按新参数重连一次，`ACN_RECENT_LINES` 改了保留已收到的报文换新上限。

`GET /api/admin/settings` 每行一个参数：`key`（env 名）、`group`、`label`、`kind`
（`bool` / `int` / `enum` / `text` / `secret`）、`value`、`source`（`db` 或 `env`）、`env`（env 里的值）。
`restart_only` 列的是改 env 重启才生效的项（端口、监听地址、数据目录），控制台只读展示。

`POST /api/admin/settings` 只写交上来的那几项。全部先校验再动手，有一项不合格就一条都不改，
400 回包里 `fields` 指出哪一项错在哪。`secret` 类的项留空表示不改；改管理口令时回包里
`admin_password_changed` 为真，旧口令立即失效。审计记一条 `settings-changed`，口令的新值不落审计。

## 报文存档

默认关。要开就在控制台「设置」里把「报文存档」打开（或 env 里写 `ACN_ARCHIVE_ENABLED=1` 再重启）。
`ACN_ARCHIVE_HOURS` 决定留多久，默认 72 小时。开着时每一行进出的报文都写进 `archive` 表，
过期的在后台删掉；关掉时 `GET /api/admin/archive` 仍然能调，只是库是空的。

## 错误

| 状态码 | `error` | 含义 |
| --- | --- | --- |
| 400 | `bad-bbox` / `bad-since` / `bad-limit` / `bad-before` | 参数写错 |
| 401 | `unauthorized` | 没带 token 或已过期 |
| 401 | `bad-credentials` | 账号或密码不对 |
| 403 | `not-authorized` | 名单里没有，或授权已过期、已暂停、已撤销；名单过期没同步时 `reason` 是 `whitelist-stale` |
| 403 | `not-public` | 台站数据没对外开（`ACN_PUBLIC_API=0`）|
| 404 | `not-found` | 台站不在 24 小时窗口里 |
| 429 | `rate-limited` | 这个 IP 调公开接口太密（`/api/public/*`） |
| 429 | `rate-limited` | 这个 token 请求太密 |

所有响应带 `Access-Control-Allow-Origin: *`，浏览器里可以直接调。

## 写一个客户端时注意

- 位置点是 24 小时滑窗，不是历史档案，别按长期记录用。
- 台站按呼号聚合，同一呼号的最新一条决定 `lat`/`lon`/`comment`。
- `packets` 是 24 小时内的报文条数，不是全站累计。
- 列表按最后听到时间从新到旧，翻页请用 `offset`，不要指望稳定游标。
