# File Service 部署交付手册（DELI）

面向 **运维 / 实施**：如何部署、配置、验收与排障本服务。  
开发细节与产品契约见 [README.md](./README.md)、[docs/configuration.md](./docs/configuration.md)。

| 项 | 说明 |
|----|------|
| 服务名 | agent-file-service（文件中台 + 文档事实层） |
| 默认端口 | **8020**（容器内 8000 → 宿主机 8020） |
| 配置分层 | **密钥/连接** → 环境变量 / `.env`；**服务行为** → `config.yaml` |
| 鉴权 | 业务 API **强制** `Authorization: Bearer` + `X-Tenant-Id`（不可关闭）；人从工作台进控制台见 UC 闸（默认关） |

---

## 1. 组件与拓扑

### 1.1 镜像

| 镜像 | 职责 | 备注 |
|------|------|------|
| **api** | HTTP API、上传/下载/查 content、入队 | 无 LibreOffice；启动时 `create_all` 建缺失表 |
| **worker** | 异步解析、旧式 Office 转换 | 含 LibreOffice；依赖队列与共享数据 |
| **valkey** | Celery 消息队列 | Docker 栈默认启用 |

### 1.2 部署模式与 Compose

本仓库**只保留一份** [`docker-compose.yml`](./docker-compose.yml)，用于**本地 / lite** 启动（SQLite + 本地盘 + Valkey；挂载源码，API `--reload`）。

**交付用 compose 不在本仓库**，由交付流程单独生成（典型为 PostgreSQL + MinIO + Valkey 的 full 拓扑）。

```text
                    ┌─────────────────┐
  调用方 ──:8020──► │  api            │
                    └────────┬────────┘
                             │ Celery / Valkey
                    ┌────────▼────────┐
                    │  worker         │  LibreOffice + 解析
                    └────────┬────────┘
                             │
              本地 lite: 共享卷 /data（SQLite · blobs · parse-results）
              交付 full: Postgres + MinIO（compose 另生成；/data 可作临时缓存）
```

**约束：**

- lite：**不要**水平扩展多个 api 副本共享同一 SQLite。
- lite 下 worker 建议 concurrency=1。
- api 与 worker 必须挂载 **同一** `file_service_data` 卷（lite）或指向同一 DB/S3（full）。

### 1.3 外部依赖（按能力）

| 依赖 | 何时需要 | 配置位置 |
|------|----------|----------|
| **LiteLLM 网关** | 图片 / 音频 / 视频 layout 与 deep；**vision-pages** 正文识别 | `LITELLM_*` + `config.yaml` 中模型名（`vision_pages_model` 等） |
| **MinerU** | `layout_parser_name=mineru`（正文识别后端） | `MINERU_API_TOKEN` + yaml |
| Valkey | Docker 默认 `PARSE_TASK_BACKEND=celery` | compose 内置 |
| Postgres / MinIO | 交付 full（compose 另生成） | 环境变量 / `.env` |

---

## 2. 快速部署

### 2.1 本地开发（`docker-compose.yml`）

```bash
cd /path/to/agent_file_service

cp .env.example .env   # 可选；本地默认 API_KEYS=dev-api-key
# 按需改服务行为（非密钥）
#    vi config.yaml

docker compose up -d --build

curl -s http://localhost:8020/health
docker compose ps
docker compose logs -f api | head -n 50
```

| 项 | 说明 |
|----|------|
| 挂载 | `./app`、`./web`、`./config.yaml` → 容器 `/app/…` |
| API | `uvicorn --reload`（保存 `.py` 自动重启） |
| Worker | 无 reload；改解析后 `docker compose restart worker` |
| 何时仍需 build | 依赖 / `Dockerfile` / 系统包（LibreOffice）变化 |
| 表结构 | api 启动 `create_all` 建**缺失表**；改列需清空 SQLite 卷 |

压缩包验收样例见 [sample/README.md](./sample/README.md)、契约 [docs/archive-expand.md](./docs/archive-expand.md)。

### 2.2 使用版本化制品镜像

```bash
./build.sh --native          # 构建 api+worker，打 tag，Trivy 扫描，写回 compose 默认 tag
set -a && source last_image_tag.txt && set +a
docker compose up -d --force-recreate

# 内网离线（x86）：导出 api + worker 两个 tar
./build.sh --amd64 --export-tar
# 内网机：
# docker load -i dist/agent-file-service-api-amd64-….tar
# docker load -i dist/agent-file-service-worker-amd64-….tar
# set -a && source last_image_tag.txt && set +a
# docker compose up -d
```

`last_image_tag.txt` 示例：

```bash
IMAGE_TAG=aarch64-20260716140000-abc1234
API_IMAGE=agent-file-service-api:aarch64-20260716140000-abc1234
WORKER_IMAGE=agent-file-service-worker:aarch64-20260716140000-abc1234
```

交付环境请使用**单独生成**的 compose（本仓库不收录），不要把本地 `docker-compose.yml` 当生产编排。

### 2.3 与本机开发进程冲突

- **不要**同时跑 `./scripts/dev.sh` 与 `docker compose` 的 api（争用 8020）。
- 本机单进程开发不在本手册范围，见 README。

### 2.4 LiteLLM 网络

compose 默认访问宿主机 `http://host.docker.internal:4000`。  
若代理在其它地址或 compose 网络：

```bash
export LITELLM_BASE_URL=http://127.0.0.1:4000
docker compose up -d
```

---

## 3. 配置说明

优先级（高 → 低）：**环境变量** → **`.env`** → **`config.yaml`** → 代码默认值。

| 配置源 | 放什么 | 运维怎么改 |
|--------|--------|------------|
| **环境变量 / `.env`** | 密钥、DB/S3/队列 URL、部署模式 | 改 `.env` 或编排系统 Secret 后 **重启** 容器 |
| **`config.yaml`** | 上传上限、解析器、自动解析开关、模型名、提示词 | 挂载文件默认 `./config.yaml:/app/config.yaml:ro`；改后 **重启 api/worker** |

容器内配置路径：`CONFIG_PATH=/app/config.yaml`。

### 3.1 密钥与租户（必须掌握）

| 变量 | 生产建议 | 说明 |
|------|----------|------|
| `API_KEYS` | **必填**，强随机；可多 key 逗号分隔 | `Authorization: Bearer <key>`。**鉴权不可关闭**。空 = 启动自动生成临时 key 并打 **WARNING 日志**（重启会变，**生产禁止依赖**） |
| `ALLOWED_TENANT_IDS` | 按需 | 空 = 调用方任意传 `X-Tenant-Id`；非空 = 仅白名单租户（含 clone 的 `target_tenant_id`） |
| `API_KEY_TENANT_MAP` | 生产推荐 | `key:tenant-a\|tenant-b` 绑定 key 可用租户；`*` = 任意租户（仍受全局白名单） |
| `LITELLM_API_KEY` | 与网关一致 | 多模态解析 |
| `MINERU_API_TOKEN` | 仅 `layout_parser_name=mineru` 正文识别时 | 可空 |

**调用方请求头（业务 API）：**

```http
Authorization: Bearer <与 API_KEYS 之一一致>
X-Tenant-Id: <租户 id>
```

- 缺/错 Bearer → **401**
- 租户不在白名单（若配置）→ **403**
- `GET /health`、`GET /metrics` **不需要**鉴权（探活 / Prometheus scrape）
- 开发控制台：`DEV_CONSOLE_ENABLED` 控制是否挂载；开启时 `/*.html` + `/shared/**` 免鉴权静态壳，业务仍走 Bearer（`config.js` / `DEV_CONSOLE_API_TOKEN`，页面不可改）
- **UC 工作台**（默认关）：`FILE_SERVICE_UC_ENABLED=1` 时人必须从 `/auth?token=` 进来；无 `X-UC-Session` 且无 L0 Bearer → `/v1` **401**。别名 `DASHBOARD_UC_*`。HMAC 密钥 `FILE_SERVICE_UC_SESSION_SECRET`（未设回落 `API_KEYS` 第一段）。生产环境禁止 `*_UC_MOCK=1`。协议：[docs/13_identity_and_access.md](./docs/13_identity_and_access.md)
- **生产**：交付栈应 **默认关闭控制台**；配置 `API_KEY_TENANT_MAP`；`quota_*` 按需限流；`/metrics` 与控制面**仅内网**；勿把真实 token 打进镜像层

本地 compose 默认 `API_KEYS=dev-api-key` 且控制台开启，**上线前务必改为生产密钥**。  
控制台（本地）：`http://<host>:8020/dashboard.html`（文档 [docs/frontend-dev-console.md](./docs/frontend-dev-console.md)）。  
工作台入口：`http://<host>:8020/auth`。本机测跳转：`FILE_SERVICE_UC_ENABLED=1` + `FILE_SERVICE_UC_MOCK=1` 后打开 `/auth?token=local-dev`。  
**内网交付**：api 镜像含 `/app/web`（worker 不含）；默认 `DEV_CONSOLE_ENABLED=false`，需要时再打开并注入 `DEV_CONSOLE_API_TOKEN`。正式对接 UC 时 `FILE_SERVICE_UC_ENABLED=1`、`MOCK=0`，配 `FILE_SERVICE_UC_BASE_URL` 与独立 `SESSION_SECRET`。

### 3.2 部署连接（按模式）

#### lite（compose 已写死常用值）

| 变量 | 典型值 |
|------|--------|
| `DEPLOY_MODE` | `lite` |
| `DATABASE_URL` | `sqlite:////data/db/file_service.db` |
| `STORAGE_BACKEND` | `local` |
| `LOCAL_STORAGE_PATH` | `/data` |
| `PARSE_TASK_BACKEND` | `celery` |
| `VALKEY_URL` / `CELERY_*` | `redis://valkey:6379/…` |

#### full

| 变量 | 说明 |
|------|------|
| `DEPLOY_MODE` | `full` |
| `DATABASE_URL` | `postgresql+psycopg://...` |
| `STORAGE_BACKEND` | `s3` |
| `S3_ENDPOINT_URL` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` / `S3_REGION` / `S3_USE_SSL` | MinIO 或对象存储 |

示例见 `.env.example`。交付 full 的连接串写在单独生成的 compose / 平台密钥里，不在本仓库。

### 3.3 服务行为（`config.yaml` 常用项）

| 字段 | 默认/说明 | 运维关注点 |
|------|-----------|------------|
| `max_upload_bytes` | 20 MiB；`0`=不限制 | 上传硬上限，超限 **413** |
| `layout_parser_name` | `markitdown` | 正文识别后端：`markitdown`·`simple`·`mineru`·`vision-pages`；`mineru`/`vision-pages` 需外网与对应密钥 |
| `litellm_base_url` | 见 yaml；可被 `LITELLM_BASE_URL` 覆盖 | 须与网关互通 |
| `vision_model` / `audio_model` / `video_model` | 多模态模型名 | 须在 LiteLLM 已配置 |
| `auto_parse_*` | 分类型自动 layout 开关与门槛 | 关某类型可减负载 |
| `startup_dependency_check` | `true` | 启动探测依赖 |
| `startup_dependency_check_strict` | `false` | `true` 时关键依赖失败**拒绝启动** |
| `legacy_office_enabled` | `true` | 旧 doc/ppt/xls 需 worker 内 soffice |
| `gc_beat_enabled` | 默认开 | API 进程内周期 GC（非 Celery Beat） |

完整字段表：[docs/configuration.md](./docs/configuration.md)。

---

## 4. 数据与持久化

### lite

| 路径（容器内） | 内容 |
|----------------|------|
| `/data/db/` | SQLite |
| `/data/blobs/` | 文件本体 |
| `/data/parse-results/` | 解析产物 |
| `/data/custom-parse-results/` | 自定义解析 |
| （derivatives 等） | 旧 Office 转换缓存 |

对应 Docker volume：`file_service_data`。

**备份：** 停写或一致快照后备份整个 volume / `/data` 目录。  
**库表：** 优先使用 full + 对象存储，或整卷搬迁；lite 勿多 api 写同一库。模型变更后 `create_all` **不会改已有列**，开发期清空 SQLite 卷即可。

### full

- 元数据：Postgres
- 对象：MinIO / S3
- 任务队列：Valkey（可按平台策略持久化）

表结构由 **api 容器启动** `create_all` 补齐缺失表；worker 不建表。数据库设计未定稿，**暂无 Alembic 迁移**。

---

## 5. 验收检查清单

### 5.1 进程与健康

```bash
docker compose ps
# api / worker / valkey 应为 healthy 或 running

curl -s http://localhost:8020/health | jq .
curl -s http://localhost:8020/metrics | head
# status: ok 或 degraded（依赖不可达时仍 HTTP 200，见 dependencies）

# 深度探测依赖
curl -s 'http://localhost:8020/health?deep=true' | jq .
```

| 现象 | 含义 |
|------|------|
| `status=ok` | 最近依赖检查通过 |
| `status=degraded` | 进程活着，但 LiteLLM/DB/Valkey 等异常；看 `dependencies` |
| 连接失败 | 端口/容器未起 |

### 5.2 鉴权与上传冒烟

```bash
# 替换为实际 API_KEYS 与租户
export KEY=dev-api-key
export TENANT=ops-smoke

curl -s -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  -F "file=@sample/outbound.xls;type=application/vnd.ms-excel" | jq .

# 无 token 应 401
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8020/v1/files
```

### 5.2.1 压缩包展开冒烟（可选）

```bash
export KEY=dev-api-key
export TENANT=ops-smoke

# tar.gz → archive.status=expanded，children ≥ 1
curl -s -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  -F "file=@sample/sample.tar.gz" | jq '.archive.status, (.archive.children|length)'

# 加密 zip（密码 secret123）
curl -s -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  -F "file=@sample/sample-password.zip" \
  -F "archive_password=secret123" | jq '.archive.status, (.archive.children|length)'

# 错密码应 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  -F "file=@sample/sample-password.zip" -F "archive_password=wrong"
```

| 期望 | 说明 |
|------|------|
| 无密码包 | `archive.status=expanded`，`children` 为可识别成员 |
| 加密 zip + 正确密码 | 同上 |
| 加密 zip 无/错密码 | HTTP **400**，`detail` 含密码提示 |
| 过多可识别成员 | `too_many`，`children=[]`，父包仍可 download |

配置项：`archive_max_expand_files` 等，见 [docs/configuration.md](./docs/configuration.md)。

### 5.3 解析链路（可选）

- 上传后查 `GET /v1/files/{id}/status`，layout 应为 completed/processing。
- 图片/音视频需 LiteLLM 可达且模型名正确；否则 `/health` 可能 degraded，解析失败见 worker 日志。

### 5.4 空 API_KEYS 注意

若生产误将 `API_KEYS` 置空：

1. api 日志出现 `Generated ephemeral API key` 与完整 key  
2. 使用该 key 可调通；**重启后 key 变化**  
3. **正确做法：** 在 Secret/`.env` 固定 `API_KEYS=...` 后滚动重启

---

## 6. 日常运维

| 操作 | 命令 / 说明 |
|------|-------------|
| 看日志 | `docker compose logs -f api` / `worker` |
| 重启 | `docker compose restart api worker` |
| 改密钥 | 更新 env → `docker compose up -d`（重建 env） |
| 改 config.yaml | 保存挂载文件 → `restart api worker` |
| 扩缩容 | lite 勿多 api；full 可多 api/worker（共享 PG/S3/队列） |
| 停服 | `docker compose down`（**加 `-v` 会删数据卷**，慎用） |

资源建议（经验值，按实际压测调整）：

| 角色 | CPU / 内存 | 说明 |
|------|------------|------|
| api | 0.5–2 核 / 512MB–2GB | 上传与元数据 |
| worker | 1–4 核 / 1–4GB | Office 转换与模型调用更吃资源 |
| valkey | 0.25 核 / 256MB+ | 按队列深度 |

---

## 7. 排障速查

| 现象 | 排查 |
|------|------|
| 8020 无响应 | `docker compose ps`；端口占用；是否与本机 uvicorn 冲突 |
| 401 Missing/Invalid Authorization | 检查 `API_KEYS` 与调用方 Bearer；是否空配置导致 key 已轮换 |
| 401 Missing X-Tenant-Id | 调用方补 header |
| 403 Tenant not allowed | `ALLOWED_TENANT_IDS` 未包含该租户 |
| 413 上传失败 | `max_upload_bytes` |
| health degraded / 图片解析失败 | LiteLLM 地址、网络、`LITELLM_API_KEY`、模型名是否在网关存在 |
| 旧 .doc/.ppt 失败 | 确认走的是 **worker** 镜像且含 soffice；看 worker 日志 |
| 解析一直 pending | Valkey/Celery；`docker compose logs worker`；队列是否堆积 |
| SQLite locked / 数据错乱 | 是否多 api 写同一 lite 库 |
| 表缺失 / schema 对不上 | 仅 api 启动时 `create_all`；改列需清空 SQLite 卷；交付 Postgres 由实施侧处理 |

查启动生成的 API key：

```bash
docker compose logs api 2>&1 | grep -i "API_KEYS\|ephemeral API key"
```

---

## 8. 安全基线（上线前）

- [ ] 生产 `API_KEYS` 为强随机，**不是** `dev-api-key`
- [ ] 鉴权无关闭开关；业务路径未暴露到免鉴权
- [ ] `LITELLM_API_KEY` / DB / MinIO 口令已轮换，不在仓库明文
- [ ] 按需配置 `ALLOWED_TENANT_IDS`
- [ ] 对外只暴露必要端口（默认 8020）；DB/MinIO/Valkey 不对公网
- [ ] 数据卷备份策略已落地
- [ ] `/health` 纳入监控；`degraded` 告警

鉴权编码规范见组织 skill：`dev-security-standard` §5.0。

---

## 9. 相关文档索引

| 文档 | 用途 |
|------|------|
| [README.md](./README.md) | 产品简介、开发启动、API 速览 |
| [docs/configuration.md](./docs/configuration.md) | 全量配置项 |
| [docs/agent-integration.md](./docs/agent-integration.md) | 智能体/调用方集成 |
| [docs/architecture-notes.md](./docs/architecture-notes.md) | 架构与鉴权摘要 |
| [.env.example](./.env.example) | 环境变量模板 |
| [config.yaml](./config.yaml) | 服务行为默认配置 |
| [docker-compose.yml](./docker-compose.yml) | 本地开发（lite + 热更）；交付 compose 另生成 |

---

## 10. 交付信息（实施填写）

| 项 | 值 |
|----|-----|
| 环境（dev/staging/prod） | |
| 部署模式 lite / full | |
| API 入口 URL | 例：`http://<host>:8020` |
| 镜像 tag（API_IMAGE / WORKER_IMAGE） | |
| API_KEYS 保管位置（Secret 名） | |
| ALLOWED_TENANT_IDS | |
| LiteLLM 地址 | |
| 数据卷 / 备份策略 | |
| 负责人 / 日期 | |
