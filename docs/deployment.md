# 部署文档

## 概览

- **平台**：Fly.io（sjc 区），Docker 容器 + 1GB persistent volume
- **域名**：`customer-profile-dec.fly.dev`（也可挂自定义域）
- **本地开发**：`uvicorn src.web.app:app --reload --port 8001` + Cloudflare tunnel（`lark-dev.finalinsight.ai`）给飞书 JSSDK 用
- **数据库**：SQLite（`/data/app.sqlite`），容器通过 `/data` volume 持久化
- **Wiki**：`/app/wiki` → `/data/wiki` 软链，容器重启不丢

## 首次部署

1. 安装 flyctl，登录：
   ```
   brew install flyctl
   flyctl auth login
   ```
2. 创建应用（已存在可跳过）：
   ```
   flyctl apps create customer-profile-dec --org <your-org>
   flyctl volumes create data --region sjc --size 1
   ```
3. 配置 secrets（一次性）：
   ```
   flyctl secrets set \
     LARK_APP_ID=cli_xxxxx \
     LARK_APP_SECRET=xxxxx \
     ANTHROPIC_API_KEY=sk-ant-xxxxx \
     APP_BASE_URL=https://customer-profile-dec.fly.dev \
     APP_ENV=prod
   ```
4. （可选，但强烈推荐）**危险接口密码**：
   ```
   flyctl secrets set DEV_REGEN_PASSWORD=<随机强密码>
   ```
   未设置 → `/followup/{id}/regen-wiki` 永久 403。
5. （可选）**Sentry 错误告警**：
   ```
   flyctl secrets set SENTRY_DSN=https://xxxx@oxxxx.ingest.sentry.io/yyyy
   ```
   没设置就 no-op，本地 dev 不受影响。免费账号 5k errors/月。
6. 首次部署：
   ```
   flyctl deploy
   ```

## 日常部署

```
git push                    # 确保代码已推
flyctl deploy               # 构建 Docker，推 registry，滚动替换
flyctl logs                 # 跟新实例日志
flyctl status               # 看机器状态
```

部署流程完全在 CI 外，没有自动部署。如果要做 CI，参考 `.github/workflows/`（暂未配）。

## Secret 清单

必填：
- `LARK_APP_ID` / `LARK_APP_SECRET` — 飞书应用凭证
- `ANTHROPIC_API_KEY` — Claude API
- `APP_BASE_URL` — 回调用，必须和你实际的外网 URL 一致（含 scheme，不含尾斜杠）
- `APP_ENV=prod` — 切换到 prod 会关掉密码登录，强制走飞书 OAuth

可选：
- `DEV_REGEN_PASSWORD` — 危险运维接口密码（不设 → 关闭）
- `SENTRY_DSN` — Sentry 错误上报
- `BH_*` — ByteHouse CRM 接入（不需要 CRM 同步就不设，scheduler 会跳过）

查看当前 secrets：
```
flyctl secrets list
```

## 运维接口

### 手动触发 AI pipeline 重跑
**场景**：某条 followup 的 AI 摘要失败了，想手动重跑。

```
curl -X POST \
  -H "X-Dev-Password: $DEV_REGEN_PASSWORD" \
  https://customer-profile-dec.fly.dev/followup/<record_id>/regen-wiki
```

返回 403 → 要么没设 `DEV_REGEN_PASSWORD`，要么密码错。每次触发都会在日志里打一行 `WARNING regen-wiki triggered by uid=xxx`。

### SSH 进容器
```
flyctl ssh console
# 里面以 app 用户（uid 1000）运行，不是 root
sqlite3 /data/app.sqlite
ls /data/wiki/
```

### 看日志
```
flyctl logs                       # 跟实时
flyctl logs --no-tail | less      # 历史
```

Fly dashboard 也有基础 metrics（CPU / 内存 / 请求数 / 状态码），免费，登 fly.io 能看到。

## 本地开发

```
# 1. 起服务
uvicorn src.web.app:app --reload --port 8001

# 2. 另起 Cloudflare tunnel（让飞书能回调 localhost）
cloudflared tunnel --url http://localhost:8001
# 或用命名隧道：cloudflared tunnel run lark-dev
```

本地 `.env` 关键项（`.env.example` 看模板）：
- `APP_ENV=dev` → 启用密码登录（密码写死 `dev`，方便本地无飞书测）
- `APP_BASE_URL=https://lark-dev.finalinsight.ai` → tunnel 的公网 URL，OAuth 回调用

## 回滚

```
flyctl releases              # 看历史版本
flyctl releases rollback vN  # 回到第 N 版（重新跑那版镜像）
```

SQLite 自身没做快照，要回滚数据得先 `flyctl ssh console` 手动备份 `/data/app.sqlite`。

## 机器规格

- `shared-cpu-2x:2048MB`（SJC）
- `min_machines_running = 1`（scheduler 常驻跑 CRM 同步 + ingest pipeline，不能 auto-stop）
- 升降配：改 `fly.toml` 里 `[[vm]] memory / cpus`，然后 `flyctl deploy`

## 已知的坑

- **Claude CLI 不能 root 跑**：`--dangerously-skip-permissions` 检测 uid=0 会拒。Dockerfile 用 `gosu` 降到 `app` 用户
- **512MB 不够**：Node 二进制冷启 >60s，会触发 `Control request timeout: initialize`。必须 ≥1GB，目前 2GB
- **飞书 OAuth 的 `offline_access` 没批**：当前只请求 `contact:user:search` scope。access_token 只活 2 小时，30 天 cookie 内用户来回用需要再过 OAuth（无 refresh 也能 fallback）
