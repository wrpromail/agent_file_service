# 开发控制台前端（SSOT）

> 改 `web/` 之前先读本文。  
> 视觉与模块结构对齐 **skillmesh** 浅色控制面：零 CDN、原生 ESM、MPA。  
> **不是**业务网盘，**不是** Agent 对话 UI。

## 1. 产品定位

| 是 | 不是 |
|----|------|
| 开发/联调内窥镜（文件、解析状态、content、指标） | 面向终端用户的文件管理 |
| 现有 REST 的投影与操作壳 | 新 BFF 业务层 |
| 部署时写入鉴权配置 | 页面上改 API Key / Tenant |

## 2. 信息架构

主导航：

```text
大盘 | 文件 | 上传 | 会话 | 指标 | 调试
```

| 路由 | 职责 |
|------|------|
| `/auth` | UC 工作台落地（剥 `?token=`、换票；成功后回 `/`） |
| `/` → `/dashboard.html` | KPI、依赖健康、最近文件 |
| `/files.html` | 列表 / 筛选 / 分页；batch-status 补 layout 徽章 |
| `/file.html?id=&tab=` | 概览 / 解析 / Content / Custom-parse / Raw |
| `/upload.html` | multipart + from-url 沙箱 |
| `/session.html?id=` | 会话附件列表 |
| `/metrics.html` | 解析 `GET /metrics` |
| `/playground.html` | 预设场景 + 自由请求 |

业务 API 仍为 `/v1/**`，须 `Authorization` + `X-Tenant-Id`。

## 3. 鉴权（无页面编辑）

| 规则 | 说明 |
|------|------|
| 开关 | `DEV_CONSOLE_ENABLED`（代码/`config.yaml` 默认 `true`；**交付 compose 默认 `false`**） |
| Token / Tenant | `web/shared/config.js` 的 `apiToken`、`tenantId`；或入口环境变量 `DEV_CONSOLE_API_TOKEN` / `DEV_CONSOLE_TENANT_ID` 注入 |
| 页面 | **无**密钥输入框；顶栏只读展示 tenant 与连接状态 |
| 生产 | **默认关闭控制台**；若开启则用入口注入，**勿把真实 secret 打进镜像层 / 提交仓库** |
| 匿名 | 控制台开启时：HTML 壳、`/shared/**`；另有 `/health`、`/metrics`、`/api/auth/uc/*` |
| 业务 | 一律 401/403，除非 Bearer + tenant 合法 |
| **UC 闸** | 默认关。`FILE_SERVICE_UC_ENABLED=1` 时无票全屏「需要统一登录」；落地 `/auth`（兼 `/#/auth`、`/?token=`）。见 [13_identity_and_access.md](./13_identity_and_access.md) |

默认示例（本地）：

```js
apiToken: "dev-api-key",
tenantId: "agent-system-a",
```

须与服务端 `API_KEYS` / 租户白名单一致。交付开启控制台示例：

```bash
DEV_CONSOLE_ENABLED=true
DEV_CONSOLE_API_TOKEN=<与 API_KEYS 之一一致>
DEV_CONSOLE_TENANT_ID=agent-system-a
```

## 4. 目录

```text
web/
  index.html
  auth.html
  dashboard.html
  files.html
  file.html
  upload.html
  session.html
  metrics.html
  playground.html
  shared/
    config.js
    styles/   # tokens.css + app.css + pages.css
    js/
      api.js
      auth.js
      uc.js / uc-gate.js / uc-strip.js
      chrome.js
      utils.js
      components/
      pages/
```

约定：原生 ESM、无打包器、零 CDN；静态资源 `?v=` cache-bust。

## 5. 后端挂载

`app/main.py`：

- 仅当 `DEV_CONSOLE_ENABLED=true` 且 `web/` 存在时挂载
- HTML 路由 → `FileResponse`（含 `/auth`）
- `/shared` → `StaticFiles`（**无**整树 `/static` 挂载）
- 各页 `<head>` 同步加载 `uc-strip.js`；模块启动前 `await runUcGate()`
- worker 镜像不含 `web/`（仅 api stage `COPY web`）

Docker API 镜像 `COPY web ./web`；compose 开发挂载 `./web`。

## 6. Prometheus 指标（与 UI）

| 指标 | 说明 |
|------|------|
| `uploads_total` / `upload_bytes_total` / `dedup_hits_total` | 上传（含 from-url 成功路径） |
| `downloads_total` / `deletes_total` | 下载 / 软删 |
| `from_url_total{result}` | URL 导入；`result=ok\|client_error\|error`。**from_url ⊂ uploads**（成功时两者都会 +1，大盘勿简单相加） |
| `archive_expand_total{status}` | 压缩包展开 |
| `parse_started_total` / `parse_completed_total` / `parse_failed_total` | 解析（profile） |
| `content_reads_total{profile,ready}` | 读 content（304 不计） |
| `custom_parse_*_total{media_kind}` | 自定义解析 |
| `quota_rejects_total{code}` | 配额 |
| `http_requests_total{method,route_group,status_class}` | HTTP 粗计数（含未捕获异常记为 5xx） |

`GET /metrics` 免鉴权，**仅内网 scrape**。指标页解析 text exposition，不替代 Grafana。

## 7. 本地访问

```bash
./scripts/dev.sh
# 浏览器
open http://127.0.0.1:8020/dashboard.html
```

编辑 `web/shared/config.js` 后硬刷新即可（开发 compose 挂载 `./web`）。

## 8. Lint

```bash
# 前端（ESLint，零 CDN ESM）
npm install          # 首次
npm run lint
# npm run lint:fix

# 后端
ruff check app tests scripts
```

## 9. 构建与内网镜像（静态资源必进 api 镜像）

- Dockerfile：**仅 api stage** `COPY web ./web` + 构建期 `RUN test`；worker 不含 `web/`
- `build.sh`：api 镜像构建后 `docker run` 校验 `/app/web`（HTML ≥8、shared/config/styles/js、uc-*.js）
- 镜像内路径：`/app/web`；进程在 `DEV_CONSOLE_ENABLED=true` 时挂载为 `/dashboard.html`、`/shared/**`
- 内网：`./build.sh --amd64 --export-tar` → `docker load -i dist/*.tar`；交付默认关控制台，开启见 §3

缺失 `web/` 时 **api 构建失败**，避免交付空壳镜像。
