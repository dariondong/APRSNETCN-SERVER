# 部署

目标机器：Ubuntu 22.04（Debian 系都通），python3 自带即可，不需要装包。

## 1 目录与环境变量

```bash
sudo mkdir -p /opt/APRSNETCN-SERVER /var/lib/aprsnetcn
sudo rsync -a --exclude .git ./ /opt/APRSNETCN-SERVER/
sudo cp deploy/aprsnetcn.env.example /etc/aprsnetcn.env
sudo chmod 600 /etc/aprsnetcn.env
```

`/etc/aprsnetcn.env` 里最少改这几项：`ACN_ADMIN_PASSWORD`、`ACN_MANAGER_SECRET`，
第一次启动再给一组 `ACN_BOOTSTRAP_*` 把第一条授权建出来。
之后 passcode 校验、鉴权、保留时长、存档、外部拉取这些参数在控制台「设置」页改，
值存进库，重启也还在；env 里的同名变量当默认值用，控制台能一键退回。
端口、监听地址、数据目录仍然只认 env。

数据库在 `ACN_DATA_DIR`（默认 `/var/lib/aprsnetcn`），备份就是拷 `aprsnetcn.db`。

## 2 systemd

```bash
sudo cp deploy/aprsnetcn.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aprsnetcn
sudo systemctl status aprsnetcn
journalctl -u aprsnetcn -f
```

服务是 `Restart=always`，改完环境变量 `systemctl restart aprsnetcn`。

## 3 端口

| 端口 | 用途 | 要不要对外 |
| --- | --- | --- |
| 14580 | 客户端接入（APRS-IS 兼容） | 要 |
| 24580 | 同上，TLS（配了证书才开） | 可选 |
| 8899 | 台站接口与管理控制台 | 要 |

管理控制台与公开接口在同一个端口上，靠 `X-ACN-Token` 区分。介意的话用 nginx
只放 `/api/v1/` 出去，见 `deploy/nginx.conf`。

## 4 nginx 反代（可选）

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/aprsnetcn
sudo ln -s /etc/nginx/sites-available/aprsnetcn /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

想上 HTTPS 就配 `certbot`。国内服务器绑域名要先有备案，没有备案只能按 IP 访问。

## 5 上线后自己验一遍

```bash
python3 tests/selftest.py                       # 离线自测
curl -s http://127.0.0.1:8899/api/v1/health     # 服务在跑
curl -s http://127.0.0.1:8899/api/v1/version
```

再用一台客户端连 14580，发一条位置报文，然后：

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8899/api/v1/stations
```

能看到刚发的台站就通了。

## 排错

- 客户端连上就被踢：看控制台审计里 `login-rejected` 的 reason。`not-in-whitelist`
  是名单没有，`auth-required` 是这条记录设了口令但客户端没带 `auth`，
  `auth-failed` 是账号密码不对。
- 客户端能登录但收不到别人的报文：多半是过滤器写窄了，或者对方发的时候
  自己这条连接还没登录。控制台「当前连接」里能看到每条连接的过滤器。
- 台站列表空：位置报文没解出来。`# aprsnetcn: bad packet` 会回到客户端，
  同时进审计的 `packet-rejected`。
