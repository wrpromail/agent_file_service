---
title: File Service · 身份与鉴权
filename: 13_identity_and_access.md
last_updated: "2026-08-20"
---

# 身份与鉴权（Identity & Access）

本系统**不提供**用户名/密码登录页。谁能打开开发控制台，由外面的管控层决定：正式路径是 **集中统一工作台跳转（UC）**；关闸时入口应仍在外部网关（basic auth / SSO）。协议为工作台 `?token=` → `token-auth` → 本地 `X-UC-Session`，按本仓 FastAPI + 静态 MPA 裁剪。

部署开关见 [`DELI.md`](../DELI.md)。配置变量见 [`configuration.md`](./configuration.md)。控制台见 [frontend-dev-console.md](./frontend-dev-console.md)。

---

## 1. 两层身份

| 层 | 载体 | 作用 |
|----|------|------|
| **服务门禁（L0）** | `Authorization: Bearer <API_KEYS>` + `X-Tenant-Id` | 机器 / Agent / 编排：`/v1/files*`、`/v1/sessions*`。**始终开启、不可关闭** |
| **终端用户（L2）** | 开闸后：`X-UC-Session`（本地 HMAC 票）+ `X-User-Id` | 人从工作台进来；浏览器**不把平台 JWT** 放进 `Authorization` |

UC **不是** OAuth / OIDC / CAS。工作台新窗口打开本应用并附带 `?token=`（也认 `workbench_token`）。本系统只调 UC `GET /uc/api/login/token-auth` 验人，再签发本地票。

**本期不做：** 分角色、拉菜单挡 API、`get-user-info` / 用户目录同步、Cookie、滑动续期、UC `refresh-token` / `logout`。换票只认「这人从工作台跳进来了」。

---

## 2. 三种入口模式

| 模式 | 开关 | 谁能打开控制台 / 无 L0 的业务 API | 何时用 |
|------|------|-----------------------------------|--------|
| **本机开发** | `FILE_SERVICE_UC_ENABLED=0`（默认） | 与现在一样 | 开发机到不了 UC |
| **工作台跳转** | `=1` | 必须带有效 `X-UC-Session`；无票 **401** | 内网正式对接 |
| **网关回退** | `=0` | 外部反向代理挡住入口；本仓不验 UC | UC 对接失败时先保「被管控」 |

关闸 **不等于** 可以匿名上内网。

也认 `DASHBOARD_UC_*` 环境变量别名。**`FILE_SERVICE_UC_*` 优先**。闸门**只认 env**，不读 `config.yaml`。

---

## 3. 跳转与换票

```text
工作台 --新窗口--> http://<host>:8020/auth?token=...
                     uc-strip.js 立刻剥掉地址栏 token（防泄露）
                     GET  /api/auth/uc/status
                     POST /api/auth/uc/exchange  {token}   ← 平台 JWT 只放 body
                              │
                              ▼
                    API 调 UC token-auth → 取 userCode（兼容 usreCode）
                    签发 HMAC 票 v1.{uid}.{jti}.{exp}.{sig}
                              │
                              ▼
              sessionStorage.afs_uc_session 持 session_ticket
              后续请求：X-UC-Session + X-User-Id
              控制台业务调用仍带既有 L0 Bearer（config.js）
```

工作台应用入口配：

- `http://<host>:8020/auth`（推荐）
- 也兼容 `/#/auth`、`/?token=`（会 hop 到 `/auth`）

平台 token **不要**放进 `Authorization`（那是 L0 服务票的位置）。

换票成功后：前端跳 `/`（再进 `/dashboard.html`）。`runUcGate` 通过才挂载控制台。开闸未登录**不渲染页面**。

---

## 4. 本地票

- 格式：`v1.{userId}.{jti}.{exp}.{sig}`；HMAC-SHA256 签 `userId:jti:exp`
- TTL 默认 **8h**（`FILE_SERVICE_UC_SESSION_TTL_SECONDS`），**无滑动续期**
- 密钥：`FILE_SERVICE_UC_SESSION_SECRET`；未设则回落 `API_KEYS` 第一段
- 单会话互斥：当前 `jti` 存在 **进程内 Map**（无用户表）。同用户再次换票，旧票立即 401。API **重启后须重新从工作台进入**
- 前端存 `sessionStorage`（非 Cookie）。API 返回 401 且 `detail` 匹配 `/uc session/i` 时清票，全屏「需要统一登录」

多副本部署时 jti **不共享**：每个 api 实例各自一份。lite 不要水平扩展；full 水平扩展前不要假设互斥跨实例生效。

---

## 5. 开闸后谁要票

**必须带 `X-UC-Session`：** 给人看的控制台（前端闸）以及 **未带有效 L0 服务票** 的 `/v1/*` 业务 API。无票 **401**，`detail` 为 `uc session required` 或 `uc session invalid or expired`。

**豁免 UC 票：**

| 路径 / 条件 | 原因 |
|-------------|------|
| `GET /health` | liveness，不含「开闸才给看」的依赖深探门槛 |
| `GET /metrics` | 内网 scrape（机器） |
| `GET /api/auth/uc/status` | 前端探测闸门 |
| `POST /api/auth/uc/exchange` | 换票本身 |
| `GET /` · `/auth` · `/*.html` · `/shared/**` | 静态壳，由 JS 闸拦截渲染 |
| 有效 `Authorization: Bearer` 且在 `API_KEYS` 内 | 既有机器 / Agent / webhook；**不把 UC 票当 L0** |

控制台开闸后仍用 `config.js` 的 Bearer 调 `/v1`（L0），同时附带 `X-UC-Session`。Agent 只持 L0，不必换 UC 票。

---

## 6. 环境变量

| 变量 | 默认 | 含义 |
|------|------|------|
| `FILE_SERVICE_UC_ENABLED` / `DASHBOARD_UC_ENABLED` | 关 | **整栈闸门** |
| `FILE_SERVICE_UC_BASE_URL` / 同上别名 | `http://127.0.0.1` | UC 前缀（须按部署覆盖） |
| `FILE_SERVICE_UC_USER_ID_CLAIM` | `userCode` | 写入 `X-User-Id` 的字段（兼容 `usreCode`） |
| `FILE_SERVICE_UC_TOKEN_QUERY` | `token` | 状态接口回传的 query 名；前端另认 `workbench_token` |
| `FILE_SERVICE_UC_MOCK` / 同上别名 | 关 | 本机假换票；**内网禁止** |
| `FILE_SERVICE_UC_MOCK_USER_ID` | `local-dev` | mock 默认工号（短 token 可直接当 userCode） |
| `FILE_SERVICE_UC_SESSION_TTL_SECONDS` | `28800` | 本地票 TTL |
| `FILE_SERVICE_UC_TIMEOUT` | 10s | 调 UC HTTP 超时（`>100` 当毫秒） |
| `FILE_SERVICE_UC_SESSION_SECRET` | （回落 `API_KEYS` 第一段） | HMAC 密钥 |
| `FILE_SERVICE_UC_APP_CODE` | 空 | 二期菜单占位，本期不拉 |

本机测跳转（不必连 UC）：

```bash
FILE_SERVICE_UC_ENABLED=1
FILE_SERVICE_UC_MOCK=1
# 打开 http://127.0.0.1:8020/auth?token=local-dev
```

内网正式：`ENABLED=1`，`MOCK=0`，配 `BASE_URL` 与 HMAC secret。

只读 curl 在开闸后（无 L0 时须带票；Agent 继续只带 Bearer）：

```bash
TICKET=...   # POST /api/auth/uc/exchange 返回的 session_ticket
curl -s http://<host>:8020/v1/files \
  -H "X-UC-Session: $TICKET" \
  -H "Authorization: Bearer <api_key>" \
  -H "X-Tenant-Id: <tenant>"
```

`pytest tests/test_uc_auth.py` 覆盖票校验、MOCK 换票、jti 互斥与 HTTP 闸门（不打真 UC）。

---

## 7. 落点

| 文件 | 职责 |
|------|------|
| [`web/shared/js/uc-strip.js`](../web/shared/js/uc-strip.js) | 各 HTML `<head>` 同步剥 `?token=` / hash，stash 后 hop `/auth` |
| [`web/shared/js/uc.js`](../web/shared/js/uc.js) | status / exchange / `sessionStorage` / 请求头 |
| [`web/shared/js/uc-gate.js`](../web/shared/js/uc-gate.js) | 开闸无票全屏拦截；`/auth` 换票成功后跳 `/` |
| [`web/auth.html`](../web/auth.html) | 落地页 |
| [`app/uc/`](../app/uc/) | 配置、HMAC 票、UC 客户端、换票与闸门 |
| [`app/uc/middleware.py`](../app/uc/middleware.py) | FastAPI 入口闸 |
| [`app/api/routes/auth_uc.py`](../app/api/routes/auth_uc.py) | `/api/auth/uc/status` · `/exchange` |

---

## 8. 残余风险

- 控制台 `config.js` 仍可能含 L0 Bearer（`DEV_CONSOLE_API_TOKEN`）。UC 保护的是「人从哪进来」；**不要**把生产密钥打进镜像层。
- 进程内 jti 在多副本时不共享。
- `GET /metrics` 仍免鉴权，**仅内网 scrape**。
