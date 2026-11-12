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

## 个人数据同步（消息 / 备份 / 历史轨迹）

一个人的三类本机数据可以存到这台服务器上，换设备或重装之后拉回来：

- **消息**：收发的私聊与群聊记录，按条并集。
- **云端备份**：整份备份文件（设置、台站、消息、群聊、翻译、荣誉、主题），每人最多三份。
- **历史轨迹**：按天存，保留一年，多台设备各自上传、读的时候合成一条。

账号就是名单里那条记录的呼号与口令（与控制台里发的账号一致），不另开注册。

### 登录换令牌

```bash
curl -s -X POST http://8.138.199.11:8899/api/v1/sync/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"bg7lzq","password":"……",
       "device":{"id":"9f2c…","ver":"2.1.41","platform":"android","abi":"arm64-v8a"}}'
```

```json
{"ok": true, "token": "…", "call": "BG7LZQ", "device": "9f2c…",
 "expires": "2027-02-04T13:00:00+08:00"}
```

`device.id` 是客户端自己的安装标识（随机生成一次、存在本机），`ver` / `platform` / `abi`
只是让人在控制台里认得出哪台是哪台。令牌默认 90 天，同一台设备再登录一次就换新的，
旧的仍然有效到过期；要停用一台设备就调下面的设备接口。

后面所有请求都带 `Authorization: Bearer <token>`。这套令牌与 `/api/v1/stations` 的
token 是两套：前者长效、落库，后者 12 小时、只在内存里。

**目前这台机器是 HTTP**，口令与数据都不过 TLS。同步页在登录前会如实提示这一点。

### 状态

```
GET /api/v1/sync/state
```

回这个呼号在云端有什么：`devices`（设备指纹 + 版本 + 最后同步时间）、
`messages`（条数与最新一条的时间）、`tracks`（天数、最早与最新一天、总点数）、
`backups`（每份的 `id` / `created` / `label` / `size`）。客户端先看它决定要不要拉。

### 消息

```
POST /api/v1/sync/messages        {"items":[{"mid":"…","ts":1793942184000,
                                   "peer":"BG6TA","dir":"in|out","text":"…",
                                   "acked":false,"grp":"…"}]}
GET  /api/v1/sync/messages?since=<毫秒>&limit=500&offset=0
```

`mid` 由客户端生成（呼号 + 方向 + 时间 + 文本的指纹），同一条消息在两台设备上算出来一样，
所以并集不会重复。`acked` 只往「已确认」上推，不会被后到的 `false` 改回去。
每人留最近 `ACN_SYNC_MSG_LIMIT`（默认 5000）条，超出从最早删。

读的那一侧带 `total`（这个人云上一共多少条）与 `more`（后面还有没有一页），
单页最多返 2000 条，拉整份历史就照着 `offset` 翻。每条多一个 `src`：
`client` 是客户端自己传上来的，`aprs` 是服务器看着报文记下的。

### 服务器自己记的消息

手机不开机、消息落在空中的那一段，过去没人记。现在服务器在转发与外部拉取的
当口顺手记一份，写进同一个人的消息里：

- 收件人是名单里的呼号 → 记一条「收到的」，`peer` 是发件人。
- 发件人是名单里的呼号 → 记一条「发出的」，`peer` 是收件人。
- 对方回的 `ack` / `rej` → 把自己发的那一条的 `acked` 标成已确认（靠报文自带的 `{编号` 对上）。

只用报文自己带的东西：正文取 `:收件人:正文` 里那一截，编号取 `{编号`。
同一句话在两台设备、两条链路上各看到一遍时，按（呼号、方向、对象、正文、时间）
判成同一条，只留一行；客户端此前已经传过的那条也不会再记一遍。
开关是 `ACN_MSG_CACHE`（默认开），每人留多少条看 `ACN_MSG_CACHE_LIMIT`（默认 0，不裁）。

### 历史轨迹

```
PUT    /api/v1/sync/tracks/{day}   {"points":[{"t":…,"lat":…,"lng":…,"v":…,"c":…,"a":…,"acc":…,"hr":…}]}
GET    /api/v1/sync/tracks                    这人的全部天：day / points / devices / updated
GET    /api/v1/sync/tracks/{day}[?device=…]   那一天的点；不带 device 就是多台合起来的
DELETE /api/v1/sync/tracks/{day}[?device=…]   删那一天（或那一台设备的那一天）
```

`day` 是本地日期 `YYYY-MM-DD`。**按设备分开存**：同一个呼号的两台设备同一天各存一行，
谁也不覆盖谁。上传是并集（同一时间戳去重，字段多的那条优先），重复上传同一批点不会变多。

读回时默认把这一天所有设备的点合成一条线：按时间排，时间差 3 秒内、距离 50 米以内的两个点
算同一个点，只留一条（两台手机同时记录同一趟路就是这个情形）。想只要某一台的就带 `device=`。

每人保留 `ACN_SYNC_TRACK_DAYS`（默认 365）天，单天不超过 `ACN_SYNC_TRACK_POINTS`（默认 60000）点。

### 云端备份

```
PUT    /api/v1/sync/backup?label=phone    请求体就是那份备份本身（JSON）
GET    /api/v1/sync/backup                列表（id / created / label / size）
GET    /api/v1/sync/backup/{id}           原样取回那份备份
DELETE /api/v1/sync/backup/{id}
```

备份不合并，每台设备上传的是一份独立的档，最多留 `ACN_SYNC_BACKUPS`（默认 3）份，
新的进来时最旧的一份丢掉。`label` 只是备注（设备名、时间由客户端自己写）。

### 设备与清空

```
DELETE /api/v1/sync/devices/{device}   停用这台设备：它的令牌全部作废，数据留着
DELETE /api/v1/sync                     清空这个呼号的云端数据（令牌不动，会话还在）
POST   /api/v1/sync/logout              只作废当前这个令牌
```

### 实现细节

- 请求体上限 `ACN_HTTP_MAX_BODY`（默认 8 MB），支持 `Content-Encoding: gzip`。
- 轨迹与备份在库里是 gzip 存的，一天几千个点也就几十 KB。
- 同步接口跟其它带 token 的接口共用每分钟 `ACN_API_RATE_MINUTE`（默认 120）的限额。
- 整台关掉：`ACN_SYNC_ENABLED=0`，所有 `/api/v1/sync/*` 回 `403 sync-disabled`。

## 自助注册

有的服务器对外开注册：自己开一个账号，注册完就能连、能换 token。开没开先问一句。

```bash
curl -s http://8.138.199.11:8898/api/v1/register
```

```json
{"ok": true, "open": true, "full": false, "accounts": 12, "max": 0,
 "password_min": 6, "server": "APRSNETCN-OPEN", "tcp_port": 24580}
```

注册：

```bash
curl -s -X POST http://8.138.199.11:8898/api/v1/register \
  -H 'Content-Type: application/json' \
  -d '{"call":"BG7XXX","password":"……"}'
```

```json
{"ok": true, "call": "BG7XXX", "account": "bg7xxx", "passcode": 19888,
 "version": 13, "server": "APRSNETCN-OPEN", "http": "http://8.138.199.11:8898",
 "tcp_port": 24580}
```

- `call` 写主呼号，带 SSID 也只记主呼号。
- `account` 不写就取呼号小写。写了要 3 到 32 位小写字母、数字、`_`、`.`、`-`。
- `password` 至少 `password_min` 位（默认 6），最多 128 位。
- 注册即授权。登录时带 `auth <账号> <口令>`，没带时按 passcode 放行（`auth_mode` 为 `optional`）。
- 一个 IP 一小时最多注册 `ACN_REGISTER_IP_HOUR` 次（默认 5），没注册成的那几次也算。
- 总人数上限是 `ACN_REGISTER_MAX`，0 表示不限。
- 呼号已经在名单里回 `409 call-taken`，账号名被占了回 `409 account-taken`。
- 没开注册的服务器回 `403 register-closed`。

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
| 400 | `bad-call` / `weak-password` / `long-password` / `bad-account` | 注册时呼号、口令或账号名不合格 |
| 409 | `call-taken` / `account-taken` | 这个呼号或账号名已经有人注册了 |
| 403 | `register-closed` / `register-full` | 这台服务器没开注册，或人数已经满了 |
| 400 | `bad-items` / `bad-points` / `bad-day` / `bad-json` / `empty-body` | 同步接口的请求体写错，或轨迹日期不是真日期 |
| 401 | `unauthorized` | 没带 token 或已过期 |
| 401 | `bad-credentials` | 账号或密码不对 |
| 403 | `not-authorized` | 名单里没有，或授权已过期、已暂停、已撤销；名单过期没同步时 `reason` 是 `whitelist-stale` |
| 403 | `not-public` | 台站数据没对外开（`ACN_PUBLIC_API=0`）|
| 403 | `sync-disabled` | 这台服务器没开个人数据同步（`ACN_SYNC_ENABLED=0`）|
| 404 | `not-found` | 台站不在 24 小时窗口里 |
| 429 | `rate-limited` | 这个 IP 调公开接口太密（`/api/public/*`） |
| 429 | `rate-limited` | 这个 token 请求太密 |

所有响应带 `Access-Control-Allow-Origin: *`，浏览器里可以直接调。

## 写一个客户端时注意

- 位置点是 24 小时滑窗，不是历史档案，别按长期记录用。
- 台站按呼号聚合，同一呼号的最新一条决定 `lat`/`lon`/`comment`。
- `packets` 是 24 小时内的报文条数，不是全站累计。
- 列表按最后听到时间从新到旧，翻页请用 `offset`，不要指望稳定游标。
