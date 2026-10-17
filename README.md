# APRSNETCN SERVER

按 APRSNET CN CONNECT 草案（v0.1.0，见 cn 站 `protocol.html#proto-head`）实现的一台 APRS 服务器：
客户端用 APRS-IS 的老办法连上来，呼号在名单里才放行，报文在服务器里实时转发，
另外把最近 24 小时的台站位置留出来，供客户端通过一个标准 HTTP 接口读走。

纯 Python 标准库，不装第三方包。Python 3.8 以上。

## 它能做什么

- TCP 接入，登录行兼容 APRS-IS：`user <呼号> pass <Passcode> [vers <名> <版本>] [filter …]`。
- 想在纯 passcode 之外再验一道，就在登录行末尾加 `auth <账号> <密码>`。
- 呼号白名单：待审核、已授权、已过期、已暂停、已撤销五种状态，带生效与失效时间。
- 报文实时转发，每条连接自己的 APRS-IS 过滤器（`r/ m/ b/ p/ t/ d/ f/ s/ q/ u/ a/`）。
- 最近 24 小时的台站位置，HTTP JSON 接口读，OpenAPI 3 描述见 `docs/openapi.json`。
- 一张台站页 `/stations`：世界地图上画 24 小时里的台站，点一个点看详情与轨迹。
  不接地图瓦片，轮廓是 Natural Earth 110m 抽稀出来的（`aprsnetcn/coast.json`，
  由 `tools/gen_coast.py` 生成）。台站数据默认不对外，`ACN_PUBLIC_API=1` 才开放。
- 管理控制台：账号与名单、发布版本、审计日志、收发统计、在线连接（可以踢）、
  报文检索（最近的内存环，开了存档就查库）。

## 跑起来

```bash
python3 run.py
```

最少要配两个环境变量：数据库目录和对外端口。

```bash
ACN_DATA_DIR=/var/lib/aprsnetcn ACN_TCP_PORT=14580 ACN_HTTP_PORT=8899 python3 run.py
```

第一次启动想直接有一条能用的授权，加这三个变量，库里已有名单就不再动：

```bash
ACN_BOOTSTRAP_CALL=BG7LZQ ACN_BOOTSTRAP_ACCOUNT=bg7lzq ACN_BOOTSTRAP_PASSWORD=…
```

管理控制台地址是 `http://<服务器>:8899/`，口令是 `ACN_ADMIN_PASSWORD`。

## 客户端登录

老客户端什么都不用改，填服务器地址和端口、呼号、passcode 就行。

需要账号密码的客户端，在登录行末尾加一段：

```
user BG7LZQ pass 13023 vers APRSLocus 2.2.0 auth bg7lzq <密码> filter r/39.9/116.4/300
```

名单里这条记录设了口令时，默认 `ACN_AUTH_MODE=optional`：客户端带了 `auth` 就核对账号密码，
不带就按 passcode 放行。改成 `required` 后，带口令的记录必须带 `auth`，标准 APRS 客户端
就连不上了。没设口令的记录一直只看 passcode。

## 读 24 小时台站

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8899/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"bg7lzq","password":"…"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8899/api/v1/stations?window=24h&limit=100'
```

详细字段见 `docs/API.md`。

## 外部数据（只读拉一份 APRS-IS）

服务器自己的客户端不多时，24 小时台站列表会很空。可以把一份外部报文只读地拉进来补上，
默认不转发给客户端。草案 8.2 要求引入外部数据必须写明来源与规则，所以这几项必须显式开：

```bash
ACN_INGEST_ENABLED=1
ACN_INGEST_HOST=rotate.aprs2.net
ACN_INGEST_PORT=14580
ACN_INGEST_LOGIN=BG7LZQ          # 用来登录上游的呼号，只读
ACN_INGEST_FILTER=r/35/105/2000 # 以 35N 105E 为圆心 2000 公里
ACN_INGEST_FORWARD=0            # 1 才把外部报文转给客户端
```

`/api/v1/health` 里的 `ingest` 段能看到连上没、收了多少条、存了多少条、最后一次出错是什么。

## 测试

```bash
python3 tests/selftest.py
```

本机起一个完整实例，跑登录矩阵、转发、过滤器隔离、24 小时接口、名单同步与撤销。

## 部署

`deploy/` 里有 systemd 单元、环境变量样例和 nginx 反代配置。说明见 `docs/DEPLOY.md`。

## 现状

草案还在改，这份实现跟着草案走。协议条款与代码的对应关系写在 `docs/PROTOCOL.md`。
