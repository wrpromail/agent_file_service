# 配置参考

配置分两层：

| 来源 | 用途 |
|------|------|
| **`config.yaml`** | 服务行为、解析器、分类型自动解析规则、多模态默认提示词（可挂载进容器） |
| **环境变量 / `.env`** | 密钥与部署连接（`API_KEYS`、`LITELLM_API_KEY`、数据库 URL、S3 密钥等） |

优先级（高 → 低）：**环境变量** → **`.env`** → **`config.yaml`** → 代码默认值。

路径：

- 默认：工作目录或仓库根下的 `config.yaml`
- 覆盖：`CONFIG_PATH`（Docker 默认 `/app/config.yaml`，compose 挂载 `./config.yaml`）

实现：`app/config.py`（Pydantic Settings + YAML 源）。

---

## 环境变量（密钥与部署）

### 部署模式

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEPLOY_MODE` | `lite` | `lite`：SQLite + 本地盘；`full`：Postgres + S3/MinIO |
| `CONFIG_PATH` | _(自动探测)_ | `config.yaml` 绝对/相对路径 |
| `PARSE_TASK_BACKEND` | `local` | `local`：API 进程内 BackgroundTasks；`celery`：投递 worker（Docker 默认） |
| `SERVICE_ROLE` | `api` | 镜像入口角色：`api` 启动时 `create_all` 建缺失表；`worker` 跳过 |
| `LOG_LEVEL` / `FILE_SERVICE_LOG_LEVEL` | `INFO` | 进程日志级别。行格式 `YYYY-MM-DDTHH:MM:SS LEVEL [logger] message`；worker 行末追加 `process=`。`GET /health`、`GET /metrics` 的 access 不记。 |
| `DEV_CONSOLE_ENABLED` | `true`（代码）；本地 compose 默认 `true`；交付应关 | 是否挂载开发控制台 HTML + `/shared` |
| `DEV_CONSOLE_API_TOKEN` | _(空)_ | api 入口写入 `web/shared/config.js` 的 `apiToken`（勿打进镜像层） |
| `DEV_CONSOLE_TENANT_ID` | _(空)_ | 同上，注入 `tenantId` |

### UC 工作台跳转（人从哪进来）

闸门**只认环境变量**（不读 `config.yaml`）。本项目前缀 `FILE_SERVICE_UC_*` 优先，同时接受 `DASHBOARD_UC_*` 别名。完整协议见 [13_identity_and_access.md](./13_identity_and_access.md)。

| 变量 | 默认 | 说明 |
|------|------|------|
| `FILE_SERVICE_UC_ENABLED` | 关 | 整栈闸门。`1`/`true`/`yes`/`on` 为开 |
| `FILE_SERVICE_UC_BASE_URL` | `http://127.0.0.1` | UC 前缀，只调 `GET /uc/api/login/token-auth`；部署时覆盖 |
| `FILE_SERVICE_UC_MOCK` | 关 | 本机假换票（`?token=local-dev`）；**内网禁止** |
| `FILE_SERVICE_UC_MOCK_USER_ID` | `local-dev` | mock 默认工号 |
| `FILE_SERVICE_UC_SESSION_SECRET` | 回落 `API_KEYS` 第一段 | 本地 HMAC 票密钥 |
| `FILE_SERVICE_UC_SESSION_TTL_SECONDS` | `28800`（8h） | 本地票 TTL，无滑动续期 |
| `FILE_SERVICE_UC_TIMEOUT` | `10`（秒；`>100` 当毫秒） | 调 UC HTTP 超时 |
| `FILE_SERVICE_UC_USER_ID_CLAIM` | `userCode` | 兼容 `usreCode` |
| `FILE_SERVICE_UC_TOKEN_QUERY` | `token` | 状态回传 query 名；前端另认 `workbench_token` |
| `FILE_SERVICE_UC_APP_CODE` | 空 | 二期菜单占位，本期不拉 |

开闸后：无有效 `X-UC-Session` 且无 L0 Bearer → `/v1` **401**（`uc session required` / `uc session invalid or expired`）。`GET /health`、`GET /metrics`、`/api/auth/uc/*`、静态壳、以及有效 `API_KEYS` 的机器调用豁免。

本机测跳转：

```bash
FILE_SERVICE_UC_ENABLED=1
FILE_SERVICE_UC_MOCK=1
# http://127.0.0.1:8020/auth?token=local-dev
```

| 模式 | 典型拓扑 | 适用 |
|------|----------|------|
| **lite（Docker）** | **api + worker + Valkey**，共享 volume `/data` | 单机 Agent、演示；旧 Office 在 worker |
| **lite（本机）** | 单进程 uvicorn，`PARSE_TASK_BACKEND=local` | 日常开发 |
| **full** | api + worker + Postgres + MinIO + Valkey | 多实例、共享对象存储 |

lite 下 **不要** 多起多个 api 副本共享同一 SQLite；需要水平扩展请用 full（Postgres）。worker 在 lite 下建议 concurrency=1。

### 密钥与租户

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEYS` | _(空)_ | 逗号分隔 API Key 白名单。**Authorization 鉴权始终开启、不可关闭。** 空 / 未配置时：启动自动生成 ephemeral key 并 **WARNING 日志**打印（重启会变；生产务必显式配置）。本地 compose 默认注入 `dev-api-key`。 |
| `ALLOWED_TENANT_IDS` | _(空)_ | 可选租户白名单（逗号分隔）。空 = 调用方自报任意非空 `X-Tenant-Id`；非空 = 仅列表内可访问，且 `clone` 的 `target_tenant_id` 也须在列表内（越权 → **403**）。 |
| `API_KEY_TENANT_MAP` | _(空)_ | 可选 key→租户绑定：`key1:tenant-a\|tenant-b,key2:*`。`*` = 该 key 任意租户（仍受 `ALLOWED_TENANT_IDS`）。未配置或 key 不在 map 中 → 仅全局白名单规则。 |
| `LITELLM_API_KEY` | `sk-dev-key` | LiteLLM Gateway Key |
| `MINERU_API_TOKEN` | _(空)_ | MinerU API Token |

业务 API 请求头：

```
Authorization: Bearer <api_key>   # 必填
X-Tenant-Id: <tenant>             # 必填；受 ALLOWED_TENANT_IDS + API_KEY_TENANT_MAP 约束
```

`GET /health`、`GET /metrics` 不要求鉴权（探活 / 内网 scrape）。**勿将 metrics 对公网裸暴露。**

FastAPI 自带 `/docs`、`/redoc`、`/openapi.json` 已关闭，不对外提供交互式文档。

### 数据库 / 存储 / 队列

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `sqlite:///./data/db/file_service.db` | lite 默认 SQLite；full 用 `postgresql+psycopg://...` |
| `STORAGE_BACKEND` | `local` | `local` \| `s3` \| `memory` |
| `LOCAL_STORAGE_PATH` | `./data` | `local` 后端根目录 |
| `S3_*` | 见 `.env.example` | 仅 `s3` |
| `VALKEY_URL` / `CELERY_*` | compose 默认 `redis://valkey:6379/…` | `PARSE_TASK_BACKEND=celery` 时必填 |

容器内 lite：`sqlite:////data/db/file_service.db`，api 与 worker **挂载同一** `file_service_data` 卷（见 `docker-compose.yml`）。

可选覆盖业务项（一般改 `config.yaml` 即可）：

| 变量 | 说明 |
|------|------|
| `PARSE_SYNC` | `true` 时同步解析（测试常用） |
| `PARSE_TASK_BACKEND` | `local` \| `celery` |
| `LAYOUT_PARSER_NAME` | 覆盖**正文识别后端**（历史名；产物 profile=layout） |
| `VISION_MODEL` / `AUTO_PARSE_IMAGE_ENABLED` 等 | 覆盖 YAML 中同名字段 |

---

## config.yaml（服务与文件规则）

仓库根目录示例见 [`config.yaml`](../config.yaml)。主要字段：

### 启动依赖检查

API **lifespan** 启动时会探测当前配置会用到的外部服务，结果写入日志，并体现在 `GET /health`。

| 字段 | 默认 | 说明 |
|------|------|------|
| `startup_dependency_check` | `true` | 是否在启动时探测 |
| `startup_dependency_check_timeout` | `5` | 单项超时（秒） |
| `startup_dependency_check_strict` | `false` | `true` 时关键依赖失败则**拒绝启动**；`false` 仅打日志，`/health` 返回 `status=degraded` |

探测项：

| 依赖 | 何时检查 | 方式 |
|------|----------|------|
| database | 始终 | `SELECT 1` |
| litellm | 始终 | `/health/liveliness` + `/v1/models`（校验 vision/audio/video 模型是否在列表中） |
| valkey | `parse_task_backend=celery` | Redis `PING` |
| mineru | `layout_parser_name=mineru` | base_url 可达 + token 非空 |

`GET /health`：进程正常时 HTTP **始终 200**（不拖垮 compose liveness）；`?deep=true` 可重新探测。

### 上传

| 字段 | 默认 | 说明 |
|------|------|------|
| `max_upload_bytes` | `20971520`（20 MiB） | **单文件上传硬上限**；`0` = 不限制。超出返回 **HTTP 413**，不入库。与 `auto_parse.*.max_bytes`（仅跳过自动解析）相互独立。 |
| `batch_max_ids` | `20` | `batch-status` / `batch-content` 单次最多 reference_id |
| `quota_max_files_per_tenant` | `0` | 租户未删除引用数上限；`0`=不限；超限 **429** `quota_exceeded_files` |
| `quota_max_bytes_per_tenant` | `0` | 租户引用关联 blob size 求和上限（按引用计，共享 blob 可能高估） |
| `quota_max_concurrent_parses_per_tenant` | `0` | pending/processing 解析并发上限 |
| `from_url_enabled` | `true` | 是否允许 `POST /v1/files/from-url` |
| `from_url_timeout` | `60.0` | URL 拉取超时（秒） |
| `from_url_allowed_hosts` | _(空)_ | 逗号分隔 host 白名单；空 = 仅 SSRF 规则（禁私网/loopback） |
| `layout_parser_version` | `0.2.0` | 与 markitdown/simple 多 block 版本对齐（缓存键） |

### file_convert 兼容层

路径 `/convert*`、`/extract-text`、`/generate-excel`、`/cache/*`、`/mcp`。须 Bearer + `X-Tenant-Id`。`POST /v1/files/from-url` 的 SSRF **不受下表影响**。

| 字段 | 默认 | 说明 |
|------|------|------|
| `convert_compat_allow_private_urls` | `true` | 兼容层拉 `file_url` 是否允许私网（内网 MinIO） |
| `convert_compat_timeout` | `300` | 同步转换 / Celery 等待超时（秒） |
| `convert_compat_max_inline_bytes` | `20971520` | 响应内联 base64 上限；`0` = 不限制。超出只给 download URL |
| `convert_compat_public_base_url` | _(空)_ | 拼 `pdf_url` / `file_urls` 的对外前缀；空则用请求的 `base_url` |
| `convert_compat_session_id` | `convert-compat` | 兼容层入库引用使用的 `session_id` |

环境变量名为字段大写（如 `CONVERT_COMPAT_ALLOW_PRIVATE_URLS`），可覆盖 yaml。

### 压缩包解压（zip / tar / tar.gz）

完整说明见 **[archive-expand.md](./archive-expand.md)**。

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `archive_expand_enabled` | `true` | 上传时是否尝试展开可识别子文件 |
| `archive_max_expand_files` | `8` | 可识别成员上限；超过则 `archive.status=too_many`，只存父包 |
| `archive_max_member_bytes` | `20971520` | 单成员未压缩大小上限 |
| `archive_max_total_bytes` | `83886080` | 可识别成员合计上限（防 zip bomb） |
| `archive_supported_extensions` | 内置列表 | 逗号分隔扩展名；空 = 代码默认（含 **`.json`**、pdf/office/图片/音视频等） |

上传 Form 字段 `archive_password`：仅 zip **ZipCrypto**；**不落库、不写日志**。AES 加密 zip 首期不支持。  
`config.yaml` 中同名字段可改默认值；环境变量名大写（如 `ARCHIVE_MAX_EXPAND_FILES`）。

### 解析

| 字段 | 默认 | 说明 |
|------|------|------|
| `parse_sync` | `false` | 是否请求内同步解析 |
| `parse_task_backend` | `local` | `local` \| `celery`（Docker 用环境变量覆盖为 `celery`） |
| `layout_parser_name` | `markitdown` | **正文识别后端**：`markitdown` · `simple` · `mineru` · **`vision-pages`**（结果写入 profile=layout） |
| `layout_parser_version` | `0.2.0` | 缓存版本号（多 block 后 bump） |
| `legacy_office_enabled` | `true` | 旧式 doc/ppt/xls 经 LibreOffice 转 OOXML 再 layout |
| `legacy_office_cache_enabled` | `true` | 缓存规范化 OOXML 为派生产物（非第二引用） |
| `legacy_office_timeout` | `300` | LibreOffice 超时（秒） |
| `legacy_office_binary` | `""` | soffice 路径；空则 PATH 查找（**worker 镜像**已装） |
| `legacy_office_converter_version` | `0.1.0` | 派生产物缓存版本 |

详见 [legacy-office.md](./legacy-office.md)。LibreOffice 仅在 **worker** 镜像。

#### MarkItDown（默认 layout）

将 PDF / Office / 文本等转为 **Markdown 字符串** 写入解析缓存。

#### 智能模式（读路径，非全局配置）

| 参数 | 默认 | 说明 |
|------|------|------|
| `GET /v1/files/{id}/content?smart_mode=` | `true` | 查询参数 |

详见 [agent-integration.md](./agent-integration.md)、[processing-factory.md](./processing-factory.md)。

#### MinerU

| 字段 | 默认 |
|------|------|
| `mineru_base_url` | `https://mineru.net/api/v4` |
| `mineru_poll_interval` | `5` |
| `mineru_timeout` | `900` |
| `mineru_work_dir` | `/tmp/file-service-mineru` |

Token 仍用环境变量 `MINERU_API_TOKEN`。

#### vision-pages（正文识别后端：页图 + 多模态）

**角色**：正文识别引擎（**不是**版面/layout 引擎）。产物仍落在 API 的 `profile=layout`（基础正文槽）。

Opt-in：`layout_parser_name=vision-pages`。PDF 用 pypdfium2 栅格化；Office/表格经 LibreOffice→PDF 再栅格化；逐页调用 LiteLLM（默认模型 `kimi-k2.6:cloud`，与图片 `vision_model` 独立）。密钥仍用 `LITELLM_API_KEY`。

| 字段 | 默认 | 说明 |
|------|------|------|
| `vision_pages_model` | `kimi-k2.6:cloud` | 本正文识别后端专用多模态模型 |
| `vision_pages_parser_name` | `vision-pages` | 解析缓存 parser_name |
| `vision_pages_parser_version` | `0.1.0` | 缓存版本 |
| `vision_pages_max_pages` | `20` | 最多识别页数（0=不限制） |
| `vision_pages_render_scale` | `2.0` | pypdfium2 渲染缩放 |
| `vision_pages_max_edge_px` | `1600` | 送模型前最长边 |
| `vision_pages_timeout` | `180` | 单页请求超时（秒） |
| `vision_pages_work_dir` | `/tmp/file-service-vision-pages` | 临时目录 |
| `vision_pages_fail_fast` | `false` | 单页失败是否整单失败 |
| `vision_pages_concurrency` | `3` | 多页并行识别线程数（`1`=串行；结果仍按页序拼接） |

### 出站 HTTP 重试（LiteLLM / MinerU 共用）

实现：`app/integrations/http`（`RetryPolicy` + `call_with_retry` + httpx/requests JSON 传输）。

| 字段 | 默认 | 说明 |
|------|------|------|
| `outbound_http_max_attempts` | `3` | **总**尝试次数（含首次）；`1` = 不重试 |
| `outbound_http_backoff_base` | `1.0` | 指数退避基数（秒） |
| `outbound_http_backoff_max` | `30.0` | 退避上限（秒） |
| `outbound_http_rate_limit_wait` | `65.0` | 遇 **429** 时等待秒数（再加 jitter） |
| `litellm_max_attempts` | `0` | `0` = 继承 `outbound_http_*`；`>0` 覆盖总次数 |
| `mineru_max_attempts` | `5` | MinerU 默认 5 次；可用 `0` 继承全局 |

**会重试**：超时、连接/传输错误、HTTP **429**、**5xx**。  
**不重试**：多数 **4xx**（如 400/401/404）。  
环境变量同名大写（如 `OUTBOUND_HTTP_MAX_ATTEMPTS`、`LITELLM_MAX_ATTEMPTS`）可覆盖。

### 多模态（LiteLLM）

| 字段 | 默认 | 说明 |
|------|------|------|
| `litellm_base_url` | `http://localhost:4000` | Gateway 地址 |
| `litellm_max_attempts` | `0` | 见上表；图片/音频/视频 chat 共用 |
| `vision_model` | `qwen3.5:cloud` | 视觉模型（本地经 LiteLLM → ollama cloud） |
| `vision_parser_name` / `vision_parser_version` | `qwen-vision` / `0.1.0` | 解析缓存键 |
| `vision_timeout` | `120` | 超时（秒） |
| `vision_default_prompt` | 还原为结构清晰的 **Markdown**… | **自动识别**默认用户提示词（标题/列表/表格） |
| `audio_model` | `gemini-flash-3.5` | 音频 layout / custom-parse 模型（LiteLLM 名；需 `input_audio`；`qwen3.5:cloud` 不支持） |
| `audio_default_prompt` | Markdown：`## 概要` / `## 转写`… | 音频自动 layout 默认提示词 |
| `audio_parser_name` / `audio_timeout` | … | 音频 job 元数据 / 超时 |
| `video_model` | `gemini-flash-3.5` | 视频 layout / custom-parse（当前测试用 Gemini；内网可切 `qwen3-omni` 等） |
| `video_default_prompt` | Markdown：`## 概述` / `## 画面要点` / `## 语音与字幕`… | 视频自动 layout 默认提示词 |
| `video_parser_name` / `video_timeout` | … | 视频 job 元数据 / 超时（默认 300s） |

密钥：`LITELLM_API_KEY`。

### 自动解析 `auto_parse`

上传 `auto_parse=true` 时仍受策略约束；**显式** `POST /parse` 与 **`POST .../custom-parse` 不受限**。

```yaml
auto_parse:
  image:
    enabled: true
    profile: layout    # layout | deep | ocr（图片默认 layout=视觉引擎）
    max_bytes: 10485760
  pdf:
    enabled: true
    profile: layout
    max_pages: 100
    max_bytes: 31457280
  spreadsheet:
    enabled: true
    profile: layout
    max_sheets: 50
    max_bytes: 33554432
    also_tables: true      # 与 layout 并行入队 profile=tables
    tables_max_rows: 5000
    tables_max_cols: 64
  # office / text / audio / video / unknown …
```

| 类型 | 默认 enabled | 默认 profile | 说明 |
|------|--------------|--------------|------|
| pdf / office / text | true | layout | Markdown 保证线 |
| **spreadsheet** | true | layout | Markdown 保证线；**`also_tables=true` 时并行 `profile=tables`**（headers+rows JSON） |
| **image** | **true** | **layout** | 视觉自动识别（Agent 读 layout） |
| **audio** | **true** | **layout** | 音频自动转写（`gemini-flash-3.5` 等） |
| **video** | **true** | **layout** | 视频自动理解（`gemini-flash-3.5`；可切 omni） |
| unknown | true | layout | 未识别类型尽力转换 |

spreadsheet 专用字段：

| 字段 | 默认 | 说明 |
|------|------|------|
| `also_tables` | `true` | 上传 auto_parse 时除 layout 外再入队 `tables`（失败不阻断 layout） |
| `tables_max_rows` | `5000` | 每 sheet 最大数据行（截断进 notes） |
| `tables_max_cols` | `64` | 最大列数 |

上传响应：`auto_parse_requested` / `auto_parse_applied` / `auto_parse_skip_reason` / `auto_parse_message`。

跳过 reason：`user_disabled` · `disabled_by_config` · `exceeds_max_pages` · `exceeds_max_bytes` · `exceeds_max_sheets` · `enabled`。

### 自定义解析 API（图片 / 音频 / 视频 + 用户指令）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/files/{id}/custom-parse` | body: `{ "prompt": "...", "media_kind?": "image\|audio\|video" }` |
| GET | `/v1/files/{id}/custom-parse` | 列出任务 |
| GET | `/v1/files/{id}/custom-parse/{job_id}` | 查询单次任务（含 `result_text`） |

与共享 `parse_results` 隔离（表 `custom_parse_jobs`）。

### GC

| 字段 | 默认 | 说明 |
|------|------|------|
| `gc_beat_enabled` | `true` | API lifespan 定时 GC（非 Celery Beat） |
| `gc_expire_interval_seconds` | `3600` | 过期引用清理间隔 |
| `gc_blob_interval_seconds` | `86400` | 孤儿 blob 清理间隔 |
| `gc_processing_reclaim_seconds` | `21600`（6h） | 卡住的 `processing` / `purging` 看门狗 |
| `gc_batch_size` | `100` | 单次 expire / blob GC 批大小 |

### 高级运行时（一般无需改）

| 字段 | 默认 | 说明 |
|------|------|------|
| `sqlite_busy_timeout_ms` | `5000` | SQLite 锁等待（毫秒） |
| `db_pool_size` / `db_max_overflow` / `db_pool_timeout` | `5` / `10` / `30` | Postgres 连接池（full 模式） |
| `celery_task_time_limit` | `3600` | Celery 单任务硬超时（秒）；`0` = 不设 |

环境变量同名大写可覆盖（如 `GC_BATCH_SIZE`、`CELERY_TASK_TIME_LIMIT`）。

---

## Docker

```bash
# 本地开发（lite + 源码挂载）
docker compose up -d --build
```

本仓库只有这一份 compose。交付编排由交付流程单独生成。

### 标准构建（`./build.sh`）

双镜像固定名：`agent-file-service-api` / `agent-file-service-worker`。

```bash
./build.sh --native              # 或去掉 --native 走 buildx（构建后扫 api+worker）
# ./build.sh --native --api      # 仅 api（仍扫本次构建的镜像）
# ./build.sh --native --worker   # 仅 worker
# ./build.sh --native --skip-scan
# TRIVY_SKIP_DB_UPDATE=1 ./build.sh --native   # 复用本地漏洞库，不联网更库

set -a && source last_image_tag.txt && set +a
docker compose up -d --force-recreate
```

- compose 默认镜像：`${API_IMAGE:-agent-file-service-api:local}` / `${WORKER_IMAGE:-…-worker:local}`
- `build.sh` 成功后会额外打 `:local`，因此也可不 `source` 直接 `docker compose up -d`
- `last_image_tag.txt` 为 KEY=value（`IMAGE_TAG` / `API_IMAGE` / `WORKER_IMAGE`），已在 `.gitignore`

本地 `docker-compose.yml` 注入：

- `API_KEYS` / `ALLOWED_TENANT_IDS` / `LITELLM_API_KEY` / `MINERU_API_TOKEN`
- UC 闸（可选）：`FILE_SERVICE_UC_*` 以及 `DASHBOARD_UC_*` 别名透传
- 部署路径：`DEPLOY_MODE=lite`、`DATABASE_URL`（SQLite）、`STORAGE_*`（本地盘）
- 任务：`PARSE_TASK_BACKEND=celery`、`VALKEY_URL` / Celery broker
- 挂载：`./config.yaml`、`./app`、`./web` + 共享 volume `/data`

---

## 本地开发

```bash
conda activate dev2609        # Python >= 3.14；脚本不自动 source .venv
cp .env.example .env          # 密钥
# 编辑 config.yaml            # 模型、自动解析规则等
pip install -e ".[dev,parsers]"
./scripts/dev.sh
# → http://127.0.0.1:8020
```

### 多模态联调

```bash
# 需本机 LiteLLM（:4000）与已启动的 file-service
python scripts/test_multimodal_live.py
```

单元测试：

```bash
python3 -m pytest tests/ -v
```
