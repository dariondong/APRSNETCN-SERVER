"""运行参数。全部走环境变量，配置文件见 deploy/aprsnetcn.env.example。"""

from __future__ import annotations

import os

PROTO_VERSION = "0.1.0"


def _text(key, default):
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip()


def _int(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


# ── 数据 ──
DATA_DIR = _text("ACN_DATA_DIR", "/var/lib/aprsnetcn")
DB_PATH = os.path.join(DATA_DIR, "aprsnetcn.db")

# ── SERVER：客户端接入 ──
TCP_HOST = _text("ACN_TCP_HOST", "0.0.0.0")
TCP_PORT = _int("ACN_TCP_PORT", 14580)
TCP_PORT_TLS = _int("ACN_TCP_PORT_TLS", 0)          # 0 = 不开 TLS 端口
TLS_CERT = _text("ACN_TLS_CERT", "")
TLS_KEY = _text("ACN_TLS_KEY", "")
SERVER_ID = _text("ACN_SERVER_ID", "APRSNETCN")     # 出现在 # logresp 里

MAX_CONN = _int("ACN_MAX_CONN", 120)
MAX_LINE = _int("ACN_MAX_LINE", 512)
LOGIN_TIMEOUT = _int("ACN_LOGIN_TIMEOUT", 30)
QUEUE_LIMIT = _int("ACN_QUEUE_LIMIT", 128)
RATE_LINES = _int("ACN_RATE_LINES", 60)             # 每连接每 10 秒
RATE_WINDOW = _int("ACN_RATE_WINDOW", 10)

# compat：passcode 不对只给收（与 APRS-IS 一致）
# strict：passcode 必须对，否则拒绝
# off   ：不校验 passcode，只看白名单
PASSCODE_MODE = _text("ACN_PASSCODE_MODE", "compat").lower()

# 客户端登录呼号与报文源呼号不一致时怎么办：allow / warn / reject
SRC_POLICY = _text("ACN_SRC_POLICY", "warn").lower()

# 名单里设了口令的记录，客户端不带 auth 怎么办
# optional：不带就按 passcode 放行（标准 APRS 客户端能直接用）
# required：必须带 auth 才放行
AUTH_MODE = _text("ACN_AUTH_MODE", "optional").lower()

# 客户端报文是否补 q 构造（qAC,登录呼号），与 APRS-IS 的行为一致
ADD_Q_CONSTRUCT = _text("ACN_ADD_Q_CONSTRUCT", "1") not in ("0", "false", "no", "off")

# ── 24 小时台站 ──
RETENTION_HOURS = _int("ACN_RETENTION_HOURS", 24)
PURGE_SECONDS = _int("ACN_PURGE_SECONDS", 60)

# ── HTTP：公开 API + 管理控制台 ──
HTTP_HOST = _text("ACN_HTTP_HOST", "0.0.0.0")
HTTP_PORT = _int("ACN_HTTP_PORT", 8899)
PUBLIC_URL = _text("ACN_PUBLIC_URL", "")            # 例如 http://8.138.199.11:8899

# ── 鉴权 ──
ADMIN_PASSWORD = _text("ACN_ADMIN_PASSWORD", "")    # 空 = 控制台关闭
MANAGER_SECRET = _text("ACN_MANAGER_SECRET", "")    # 空 = 名单同步接口关闭
TOKEN_TTL = _int("ACN_TOKEN_TTL", 12 * 3600)
API_RATE_MINUTE = _int("ACN_API_RATE_MINUTE", 120)  # 每 token 每分钟
# 公开台站页与它调的那两个接口不验 token，按来源 IP 限流
PUBLIC_RATE_MINUTE = _int("ACN_PUBLIC_RATE_MINUTE", 60)

# ── 外部数据：只读地拉一份 APRS-IS 的报文，补进 24 小时台站 ──
# 草案 8.2：引入外部数据要明确来源与规则，所以默认关，打开时必须写清过滤器。
INGEST_ENABLED = _text("ACN_INGEST_ENABLED", "0") not in ("0", "false", "no", "off", "")
INGEST_HOST = _text("ACN_INGEST_HOST", "rotate.aprs2.net")
INGEST_PORT = _int("ACN_INGEST_PORT", 14580)
INGEST_LOGIN = _text("ACN_INGEST_LOGIN", "BG7LZQ")
INGEST_FILTER = _text("ACN_INGEST_FILTER", "r/35/105/2000")
INGEST_FORWARD = _text("ACN_INGEST_FORWARD", "0") not in ("0", "false", "no", "off", "")

# ── 第一次启动时的引导账号（库里已有名单就忽略）──
BOOTSTRAP_CALL = _text("ACN_BOOTSTRAP_CALL", "")
BOOTSTRAP_ACCOUNT = _text("ACN_BOOTSTRAP_ACCOUNT", "")
BOOTSTRAP_PASSWORD = _text("ACN_BOOTSTRAP_PASSWORD", "")
