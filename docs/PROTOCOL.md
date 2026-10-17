# 协议条款与实现对照

对应 cn 站 `protocol.html#proto-head` 的草案 v0.1.0。左列是条款，右列是代码位置与自测项。

## 2 系统组成

| 条款 | 实现 | 自测 |
| --- | --- | --- |
| 2.1 SERVER 收连接 | `aprsnetcn/hub.py` `Hub._accept` | 登录矩阵 |
| 2.1 解析登录 | `hub.LoginRequest.parse` | 账号密码登录、pass -1 |
| 2.1 登录校验 | `Hub._login` | 六项登录断言 |
| 2.1 白名单判断 | `store.Store.account` + `auth.may_login` | 不在名单、待审核、撤销 |
| 2.1 收发报文 | `Hub._on_line` | 转发、qAC、只读 |
| 2.1 分发数据 | `Hub.broadcast` + `filters.Filter` | 过滤器隔离 |
| 2.1 异常处理 | 行长、限速、队列上限、超时 | 只读与坏行 |
| 2.1 记录日志 | `store.audit` | 控制台审计表 |
| 2.2 MANAGER 名单维护 | `api` `/api/admin/account` | 控制台加账号 |
| 2.2 MANAGER 同步名单 | `api` `/api/manager/whitelist` | 签名、版本、重放 |
| 2.3 职责分离 | SERVER 只读本地库，名单由 MANAGER 推进版本 | 版本没前进被拒 |

## 3 客户端接入

| 条款 | 实现 |
| --- | --- |
| 3.2 登录格式 | `hub.LOGIN_RE`，`user … pass … vers … auth … filter …` |
| 3.3 校验顺序 | `Hub._login`：格式 → 呼号 → passcode → 白名单 → 状态 → 放行 |
| 3.4 Passcode | `aprslib.passcode`，策略 `ACN_PASSCODE_MODE=compat/strict/off` |
| 3.4 账号口令 | 登录行末尾 `auth <账号> <密码>`；`ACN_AUTH_MODE=optional/required` 决定不带时放不放 | 可选与强制两种模式各一条断言 |
| 3.5 拒绝接入 | `Hub._reject`，只回类别，不回内部信息 |

## 4 白名单

| 条款 | 实现 |
| --- | --- |
| 4.2 记录字段 | `accounts` 表：呼号、账号、口令哈希、状态、生效、失效、版本、更新时间 |
| 4.3 五种状态 | `auth.STATUSES`，有效期到了自动算过期 |
| 4.4 全量与增量 | `store.apply_whitelist(mode="full"/"incremental")` |
| 4.4 完整性 | `auth.sign` / `verify_signature`，HMAC-SHA256 |
| 4.4 版本连续 | `api._manager_publish`：版本必须大于当前 |
| 4.4 失败保留旧版 | 校验不过直接 403/409，不动库 |
| 4.5 撤销后断连接 | `api.Api.enforce_whitelist`：改名单、删记录、名单同步后重查在线连接，不合格的就断（`hub.kick`），记一条 `revoked-disconnect` |
| 4.6 管理服务不可用 | SERVER 只读本地库，MANAGER 掉线不影响接入 |
| 4.6 名单有效期 | `ACN_WHITELIST_MAX_AGE_HOURS`：超过这么久没同步就拒新连接（`whitelist-stale`）；默认 0 不限制，运营方自己设 |

## 5 报文处理

| 条款 | 实现 |
| --- | --- |
| 5.1 格式兼容 | `aprslib.parse`，位置、对象、条目、消息、状态、气象、第三方 |
| 5.2 检查 | 行长 `MAX_LINE`、字符集、限速 `RATE_LINES` |
| 5.3 转发 | `Hub.broadcast`，30 秒去重，队列上限 `QUEUE_LIMIT` |
| 5.4 源呼号 | `ACN_SRC_POLICY=allow/warn/reject`，与登录呼号分开判断 |
| 5.5 内存转发 | 报文只在内存里过，落库的只有 24 小时位置与审计 |

## 6 过滤器

| 条款 | 实现 |
| --- | --- |
| 6.1 支持子集 | `filters.KNOWN`：`r a m b p t d f s q u` |
| 6.2 语法与上限 | 每条连接最多 16 条，单条 200 字符，认不出的拒 |
| 6.3 隔离 | 过滤器挂在 `Client.filters`，只影响自己的发出 |
| 6.3 默认订阅 | 没带过滤器的连接收全部报文，`/api/v1/version` 里 `default_filter: all` |

## 7 安全与运维

| 条款 | 实现 |
| --- | --- |
| 7.1 接入限制 | 连接数、登录超时、行长、限速 |
| 7.2 管理隔离 | HTTP 端口与 TCP 接入分开，管理接口要口令（`X-ACN-Token`） |
| 7.3 日志 | `audit` 表，登录、拒绝、同步、改名单、踢连接、管理认证失败，不含报文正文 |
| 7.4 故障恢复 | 启动先 `PRAGMA quick_check` 再报名单版本、条数、最近同步（`main.self_check`）；名单缺失按拒绝处理；`/api/v1/health` 不带 token 就能看到名单版本与最近同步 |

## 8 网络边界

| 条款 | 实现 |
| --- | --- |
| 8.1 国内闭环 | 代码里没有向外部转发客户端报文的路径（`ACN_INGEST_FORWARD` 默认 0） |
| 8.2 外部数据 | `ingest.py`：默认关，开了只读拉上游，只进 24 小时库，转发另开；自测里验了登录行与入库 |
| 8.3 数据处理 | 位置保留 24 小时，到点删（`ACN_RETENTION_HOURS`） |

## 9 测试

| 条款 | 实现 |
| --- | --- |
| 9.1 兼容性 | `tests/selftest.py` 登录、拒绝、撤销、转发、过滤器、撤销即断连 |
| 9.2 白名单 | 全量、增量、签名错、版本重放、名单过期 |
| 9.3 安全 | 只读连接、坏报文、错口令、管理口令错留痕 |

## 与草案的三处差异

1. 草案 5.5 要求「尽量避免不必要的持久化」，24 小时台站是明确的产品功能，
   位置数据保留 24 小时后删除，正文不落库。
2. 草案没说账号口令放在哪里，本实现放在登录行末尾的 `auth` 段，与原有格式并列，
   老客户端不写这一段就完全不受影响。
3. 草案没要求留报文历史。本实现留了一个开关 `ACN_ARCHIVE_ENABLED`，默认关。
   开着时进出的整行报文写进 `archive` 表，超过 `ACN_ARCHIVE_HOURS`（默认 72）删掉；
   关掉就只在内存环里留最近几百行，重启即空。
