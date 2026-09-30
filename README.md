# File Service

多智能体系统中的**文件中台 + 文档事实层**：内容去重、租户隔离引用、分层可插拔解析。  
不是网盘，也不是业务文档 Agent。

- **运维部署交付**：[DELI.md](./DELI.md)（部署、配置、验收、排障）
- **产品原则（保证线 × 能力菜单）**：[docs/product-principles.md](./docs/product-principles.md)
- 设计：[design.md](./design.md)
- 实现文档：[docs/](./docs/)（索引见 [docs/README.md](./docs/README.md)）

### 智能体怎么用（30 秒）

| 层级 | 行为 |
|------|------|
| **保证线** | 上传即有元数据；默认后台准备 layout（Markdown，多 block） |
| **能力菜单** | ocr / deep / custom-parse 等由 Agent 工具按需 `trigger`；旧 **file_convert** 路径（`/convert*`）已兼容迁入 |
| **读路径** | `max_chars` / `outline` / `sheets` 裁剪；ETag 轮询；batch API |
| **不做** | 业务解读、RAG、业务语义切分、主动回调 |

## 要求

- **Python 3.14+**
- 依赖**不锁版本**（`pyproject.toml` 仅列包名，安装最新）

## 快速启动（默认 lite）

**镜像拆分**：

| 镜像 | 职责 | 体积敏感依赖 |
|------|------|--------------|
| **api** | HTTP 上传/下载/元数据/取 content | 无 LibreOffice |
| **worker** | layout / deep / 旧式 Office 转换 / 后续重依赖 | LibreOffice（及将来 ffmpeg 等） |

**lite**（默认 Docker）：SQLite + 本地盘 + **api + worker + Valkey**，共享 volume `/data`。  
**本机开发**：可继续单进程 uvicorn（`parse_task_backend=local`，BackgroundTasks）。

### 本地开发（推荐）

本机用 **Python >= 3.14**（例如 conda 环境 `dev2609`）。脚本不自动激活仓库内 `.venv`。

```bash
conda activate dev2609
cp .env.example .env          # 密钥与部署连接；仅首次
# 编辑 config.yaml            # 服务行为、分类型自动解析、视觉模型等
pip install -e ".[dev,parsers]"

./scripts/dev.sh              # 建 ./data、create_all、uvicorn :8020 --reload
# 健康检查
curl -s http://127.0.0.1:8020/health
# 开发控制台（本地默认开；鉴权见 web/shared/config.js；交付默认 DEV_CONSOLE_ENABLED=false）
open http://127.0.0.1:8020/dashboard.html
# 本机测 UC 跳转（不必连真 UC）：FILE_SERVICE_UC_ENABLED=1 FILE_SERVICE_UC_MOCK=1
# open http://127.0.0.1:8020/auth?token=local-dev
```

等价手写命令：

```bash
export DEPLOY_MODE=lite
export DATABASE_URL=sqlite:///./data/db/file_service.db
export STORAGE_BACKEND=local
export LOCAL_STORAGE_PATH=./data
# 默认 parse_task_backend=local（进程内后台任务）
uvicorn app.main:app --host 127.0.0.1 --port 8020 --reload
```

数据目录：`./data/`（`db/file_service.db` + blobs），已在 `.gitignore`。

端口冲突时：

```bash
pkill -f "uvicorn app.main:app" 2>/dev/null
lsof -i :8020
# 或 PORT=8021 ./scripts/dev.sh
```

### Docker 部署（本仓库仅本地 compose）

| 文件 | 场景 | 启动 |
|------|------|------|
| `docker-compose.yml` | **本地开发**（lite：SQLite + 挂载 `./app` + API `--reload`） | `docker compose up -d --build` |

交付用 compose **不在本仓库**，由交付流程单独生成。

```text
┌─────────────┐   Celery/Valkey   ┌──────────────────────┐
│  api :8020  │ ───────────────► │  worker               │
│             │                  │  LibreOffice + parse  │
└──────┬──────┘                  └──────────┬───────────┘
       │                                     │
       └──────── 数据面（lite 卷 / full PG+S3）─┘
```

**本地开发：**

```bash
docker compose up -d --build
curl -s http://localhost:8020/health
# 改 worker 解析后：
docker compose restart worker
```

**不要**同时跑 `./scripts/dev.sh` 与 compose api（会争用 8020）。

配置见 [docs/configuration.md](./docs/configuration.md)。

### 标准镜像构建与制品扫描

发布/验收请用 `./build.sh`（默认构建 **api + worker** 两个镜像）：

| 项 | 说明 |
|----|------|
| 镜像名 | 固定 `agent-file-service-api` / `agent-file-service-worker`（勿用目录名推导） |
| 标准标签 | `{name}:{arch}-{timestamp}-{git_commit}`，两镜像共享同一 `IMAGE_TAG` |
| 本地别名 | 构建成功后额外 `docker tag` 为 `{name}:local`，并对齐 compose 默认值 |
| 制品文件 | `last_image_tag.txt`（**可 source 的 KEY=value**）；compose 默认 `API_IMAGE`/`WORKER_IMAGE` 同步为版本化 tag |
| 离线 tar | `--export-tar` / `--dist` → `dist/` 下 **api + worker 两个 tar** + `last_image_tar.txt` |
| 扫描 | 对**已构建的 api / worker** 各跑 Trivy（`--scanners vuln`，HIGH/CRITICAL，`--ignore-unfixed`） |
| 漏洞库缓存 | 默认 `~/.cache/trivy`（`TRIVY_CACHE_DIR`）；两镜像共用，禁止每次全量下载 DB |
| 跳过扫描 | 本地可 `--skip-scan`；离线复扫 `TRIVY_SKIP_DB_UPDATE=1` |

`last_image_tag.txt` 格式：

```bash
IMAGE_TAG=aarch64-20260716140000-abc1234
API_IMAGE=agent-file-service-api:aarch64-20260716140000-abc1234
WORKER_IMAGE=agent-file-service-worker:aarch64-20260716140000-abc1234
```

仅 `--api` / `--worker` 时对应槽位更新，另一槽位尽量保留上次值。

```bash
./build.sh --native
# 仅 api / 仅 worker：
# ./build.sh --native --api
# ./build.sh --native --worker
# 跳过扫描（纯本地调试）：
# ./build.sh --native --skip-scan
# 离线/极速复扫（不联网更漏洞库，仍扫两镜像）：
# TRIVY_SKIP_DB_UPDATE=1 ./build.sh --native
# 内网 amd64 双镜像 tar：
# ./build.sh --amd64 --export-tar
# docker load -i dist/agent-file-service-api-amd64-….tar
# docker load -i dist/agent-file-service-worker-amd64-….tar

# 用版本号镜像起本地栈
set -a && source last_image_tag.txt && set +a
docker compose up -d --force-recreate

# 或直接吃 :local（build.sh 已打好，无需 export）
# docker compose up -d --force-recreate
```

## 服务能力：上传与格式支持

**上传**：`POST /v1/files` **不设** MIME 白名单——常见**图片**、**PPT/PPTX**、**音频**均可入库并得到 `reference_id`（与 PDF / Excel / Word 相同）。

**识别（事实层）**与上传脱钩，按类型成熟度不同：

| 类型 | 上传 | 同步元数据 | 默认自动解析 | 能力菜单 / 备注 |
|------|------|------------|--------------|-----------------|
| 常见图片 png/jpeg/webp/gif/bmp | ✅ | ✅ 尺寸等 | ✅ **layout**（视觉模型 OCR+简述，写入 layout 供 Agent 读） | `deep` 同源视觉；`custom-parse` 用户指令 |
| **`.pptx` / `.ppt`** | ✅ | office | layout → Markdown | `.ppt` 经 LibreOffice→pptx（**worker** 镜像装 soffice） |
| **`.doc` / `.xls`** | ✅ | office / spreadsheet | layout → Markdown | 旧格式经 LibreOffice→docx/xlsx（worker） |
| 金山 WPS **`.wps` / `.et` / `.dps`** | ✅ | office / spreadsheet | layout → Markdown（**尽力**） | LibreOffice→OOXML（worker）；见 [docs/legacy-office.md](./docs/legacy-office.md) |
| 音频 mp3/wav/m4a 等 | ✅ | ✅ duration 等 L0 | ✅ layout（LiteLLM 转写） | 默认 `gemini-flash-3.5`；`custom-parse` 可改指令 |
| 视频 mp4/webm/mov 等 | ✅ | ✅ duration 等 L0 | ✅ layout（LiteLLM 理解） | 默认 `gemini-flash-3.5`；内网可切 omni |
| PDF / xlsx / docx / 文本 | ✅ | ✅ 分类型 | layout → Markdown | 见下「处理工厂」 |

服务规则集中在 **`config.yaml`**（Docker 挂载）；密钥在 `.env` / compose 环境变量。详见 [docs/configuration.md](./docs/configuration.md)。

产品契约与完整矩阵：[docs/product-principles.md](./docs/product-principles.md) · 工厂路由：[docs/processing-factory.md](./docs/processing-factory.md)。

## 默认解析：文件 → Markdown（智能体友好）

默认**正文识别后端**为 `markitdown`（[Microsoft MarkItDown](https://github.com/microsoft/markitdown)，见 `config.yaml`；结果写入 `profile=layout`）：

- 上传后对 **Office / PDF / 文本 / PPTX 等** 自动 `layout` → **Markdown 字符串**
- **图片**默认自动 `layout`（引擎为 LiteLLM 视觉模型，写入 layout；默认提示词：识别文本并简要描述）
- **音视频**默认自动 `layout`（依赖网关模型；见 `config.yaml`）
- 智能体取内容：

```bash
# 上传
curl -s -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/outbound.xls;type=application/vnd.ms-excel"

# 取 Markdown（默认 smart_mode=true：Excel 空 sheet 不展示）
curl -s "http://localhost:8020/v1/files/{reference_id}/content?profile=layout" \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['metadata']['markdown'])"

# 大文档只取前 8k 字符 / 大纲 / 指定 sheet：
# .../content?profile=layout&max_chars=8000
# .../content?profile=layout&mode=outline
# .../content?profile=layout&sheets=Sheet1

# 需要原始完整 Markdown（含空白 Sheet2/Sheet3）时：
# .../content?profile=layout&smart_mode=false

# 表格结构化 JSON（xlsx/xls；与 layout 并行，auto_parse.spreadsheet.also_tables）：
# .../content?profile=tables
# → metadata.tables_json = spreadsheet_tables@v1（headers + rows per sheet）
# .../content?profile=tables&sheets=应用系统
```

| 取法 | 路径 |
|------|------|
| 推荐 | 先看 `content.ready`；为 true 时用 `content.metadata.markdown` |
| 未就绪 | `ready=false` + `message` / `message_code`（HTTP 仍 200，勿当错误码） |
| 结构块 | `content.blocks[]`（Excel 按 sheet；文档按标题/页） |
| 裁剪 | `mode=outline` · `max_chars`/`offset` · `sheets` |
| 智能模式 | `smart_mode=true`（默认）：表格丢弃无内容 sheet |

可选**正文识别后端**（`layout_parser_name`，写入 `profile=layout`）：`markitdown`（默认）· `simple`（测试）· `mineru`（高精度 PDF）· **`vision-pages`**（页图 + 多模态正文识别，默认 `kimi-k2.6:cloud`）。

## Lint

```bash
# 前端开发控制台（web/）
npm install && npm run lint

# 后端
ruff check app tests scripts
```

## 测试

```bash
conda activate dev2609
python -m pytest tests/ -q
# Python SDK（respx mock，不启服务）
pip install -e "./sdk/python[dev]"
python -m pytest sdk/python/tests -q
```

- 单元测试：SQLite 内存库 + 内存对象存储（layout 强制 `simple`）
- `test_local_storage.py`：lite 本地盘存储
- `test_streaming_upload.py`：大文件流式上传；`test_gc_race.py`：GC 与上传竞态
- `test_smart_markdown.py`：content `smart_mode`（Excel 空 sheet 过滤）
- `test_valkey.py`：仅校验配置字段（无需真实 Valkey）
- `test_uc_auth.py`：UC HMAC 票 / MOCK 换票 / jti 互斥 / HTTP 闸门（不打真 UC）

## API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活 + 依赖探测结果（LiteLLM/DB 等）；`?deep=true` 重新探测；进程正常时 HTTP 200，`status` 为 `ok`/`degraded` |
| GET | `/metrics` | Prometheus text；免鉴权（**仅内网 scrape**） |
| GET | `/dashboard.html` 等 | **开发控制台**（`DEV_CONSOLE_ENABLED`；静态 MPA，见 [docs/frontend-dev-console.md](./docs/frontend-dev-console.md)） |
| GET | `/auth` | UC 工作台落地（剥 `?token=`）；见 [docs/13_identity_and_access.md](./docs/13_identity_and_access.md) |
| GET | `/api/auth/uc/status` | UC 闸门探测（豁免会话票） |
| POST | `/api/auth/uc/exchange` | 工作台 token 换本地 `X-UC-Session`（平台 JWT 只放 body） |
| POST | `/v1/files` | 上传；可选 `session_id`、`agent_id`、`uploaded_by`、`auto_parse`、`expires_at`、`metadata`、`archive_password`（zip ZipCrypto）。zip/tar/tar.gz 可识别成员 ≤8 时展开为 `archive.children[]`（发给 Agent 用子句柄） |
| POST | `/v1/files/from-url` | URL 拉取入库（SSRF 防护） |
| POST | `/v1/files/batch-status` | 多附件状态（逐项错误） |
| POST | `/v1/files/batch-content` | 多附件 content + 裁剪参数 |
| GET | `/v1/files` | 按元数据过滤查询（`session_id`、`agent_id`、`uploaded_by`、`filename`、`content_hash`、分页） |
| GET | `/v1/files/{id}` | 引用详情与解析状态 |
| PATCH | `/v1/files/{id}` | 更新 session/agent/metadata/expires_at |
| DELETE | `/v1/files/{id}` | 软删 + ref_count-- + 存储 GC |
| POST | `/v1/files/{id}/clone` | 引用克隆：为同一 blob 在目标 tenant 下创建新引用，解析结果自动共享 |
| GET | `/v1/files/{id}/content` | 结构化内容；**未就绪 200+ready=false+message**；`profile=layout\|ocr\|deep\|tables`；`smart_mode` 去空 sheet |
| GET | `/v1/files/{id}/download` | 下载原始文件二进制 |
| POST | `/v1/files/{id}/parse` | 触发 `layout` / `ocr` / `deep` / `tables`（`force` 跳过缓存；不受 auto-parse 门槛限制） |
| POST | `/v1/files/{id}/custom-parse` | 图片/音频/视频：用户自定义 `prompt` 解析（独立 `custom_parse_jobs` 表） |
| GET | `/v1/files/{id}/custom-parse` | 列出该文件的自定义解析任务 |
| GET | `/v1/files/{id}/custom-parse/{job_id}` | 查询单次自定义解析结果 |
| GET | `/v1/files/{id}/status` | 解析进度（ETag） |
| GET | `/v1/files/{id}/archive/members` | 压缩包列目录 |
| GET | `/v1/sessions/{id}/files` | 会话内文件列表 |

完整 Agent 契约见 [docs/agent-integration.md](./docs/agent-integration.md)。

请求头（**鉴权不可关闭**）：

- `Authorization: Bearer <api_key>` — 必填；`API_KEYS` 支持逗号分隔多个 key；未配置时启动自动生成并打日志
- `X-Tenant-Id: <tenant>` — 必填；若配置了 `ALLOWED_TENANT_IDS` 则仅允许列表内租户
- 开闸后（`FILE_SERVICE_UC_ENABLED=1`）人从工作台进来须带 `X-UC-Session` + `X-User-Id`；有效 L0 Bearer 的 Agent 调用豁免。默认关闸。协议见 [docs/13_identity_and_access.md](./docs/13_identity_and_access.md)

## 跨智能体文件共享

file service 本身不做跨智能体的权限判断，由上层编排服务（Agent Runtime / A2A Gateway）负责决策后调用以下两个原语：

**按元数据查询**（`GET /v1/files`）：编排服务可在自身 tenant 内，按 `agent_id`、`uploaded_by`、`session_id` 等查找文件句柄。

**引用克隆**（`POST /v1/files/{id}/clone`）：编排服务决定 A 的文件需共享给 B 时，以 A 的 credential 调用 clone，在 B 的 `target_tenant_id` 下生成新 `reference_id`。新引用：
- 指向同一物理 blob（不重复存储）
- 自动复用已有解析结果（不重复解析）
- 各自独立生命周期（`expires_at`、软删互不影响）
- `custom_metadata` 自动注入 `cloned_from` + `source_tenant_id` 供溯源

```bash
# 编排服务将 tenant-A 的文件克隆给 tenant-B
curl -s -X POST http://localhost:8020/v1/files/{reference_id}/clone \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: tenant-A" \
  -H "Content-Type: application/json" \
  -d '{"target_tenant_id": "tenant-B", "session_id": "sess-B", "agent_id": "agent-B"}'
```

## 处理工厂

按 **文件类型 + 子类型 + 处理层级** 路由处理器（元数据 → layout → ocr → deep）。

```
spreadsheet/* → 元数据 → layout(markitdown → Markdown 表) + 默认可并行 tables(JSON)
office/*      → 元数据 → layout          # 当前一等公民：docx/doc
pdf/digital   → 元数据 → layout
pdf/scanned   → 元数据 → ocr → layout
text/*        → 元数据 → layout
image/*       → 元数据 → layout / ocr / deep
audio/*       → 元数据 → layout（转写）
video/*       → 元数据 → layout（理解）
unknown/*     → generic 元数据 → 尽力 layout
```

| 常见扩展 | 实际路径摘要 |
|----------|----------------|
| 图片 | `image/*` 一等；默认 **layout=视觉模型**；`deep` 同源；自定义指令用 custom-parse |
| `.pptx` / `.docx` | office；layout → MD |
| **xlsx / xls / csv** | spreadsheet；layout → MD；**`profile=tables` → tables_json**（`also_tables` 默认 on） |
| 旧 `.ppt` / `.doc` / `.xls` | LibreOffice 规范化后 layout（worker）；xls 亦可 tables |
| 音频 / 视频 | `audio/*` / `video/*`；L0 duration；layout 依赖 LiteLLM |

上传同步返回**分类型元数据**（页数、sheet、docx 段落/表格、编码等；未知类型仅 MIME+size）。字段契约见 [docs/metadata-extraction.md](./docs/metadata-extraction.md)；工厂路由见 [docs/processing-factory.md](./docs/processing-factory.md)。

## MinerU（可选，高精度 PDF）

```bash
LAYOUT_PARSER_NAME=mineru
MINERU_API_TOKEN=<token>
PARSE_SYNC=false
```

客户端：`app/integrations/mineru/`。

## vision-pages（可选正文识别后端：页图 + 多模态）

将 PDF / Office / 电子表格渲染为页图，经 LiteLLM 多模态**识别正文**为 Markdown（`metadata.markdown`）。  
这是**正文识别引擎**，不是版面 layout 引擎；配置仍用历史字段 `LAYOUT_PARSER_NAME`，产物落在 `profile=layout`（基础正文槽）。

```bash
LAYOUT_PARSER_NAME=vision-pages
# 可选覆盖；默认见 config.yaml
# VISION_PAGES_MODEL=kimi-k2.6:cloud
LITELLM_API_KEY=<key>   # 与 litellm_base_url 配合
```

Office/表格依赖 LibreOffice（`soffice`）；纯 PDF 仅需 pypdfium2。  
多页并行：`vision_pages_concurrency`（默认 3；`1`=串行）。对比脚本：`scripts/compare_body_engines.py`。

## 智能体集成

智能体后端请用 Python SDK（协议封装 + `wait_for_content` 等待异步 layout），见 [sdk/python/README.md](./sdk/python/README.md)。REST 与 MCP 约定：[docs/agent-integration.md](./docs/agent-integration.md) · 联调手册：[AGENT_API.md](./AGENT_API.md)。从内网 file_convert 迁入：只改 Base URL 与鉴权头，见 [AGENT_API.md §5.8](./AGENT_API.md)。

**进会话**（写入 Agent workspace）不在本服务内完成，两条路径：

- **Path A**：消息带 `reference_id` / `[file:…]`，智能体再按需拉取或物化
- **Path B**：调用方会话 API 预挂接附件

详见 [docs/agent-integration.md](./docs/agent-integration.md)。

**压缩包**：zip / tar / tar.gz 上传即解压（可识别成员 ≤8 → 多子句柄）；加密 zip 用 `archive_password`。文档：[docs/archive-expand.md](./docs/archive-expand.md) · 样例：[sample/README.md](./sample/README.md)。

## 配置

环境变量说明：[docs/configuration.md](./docs/configuration.md)

## 部署模式

| 模式 | 怎么起 | 存储 | 说明 |
|------|--------|------|------|
| **lite 本机** | `./scripts/dev.sh` | SQLite + 本地盘 | 单进程；`PARSE_TASK_BACKEND=local`；GC 在 API lifespan |
| **本地 Docker** | `docker compose up -d` | lite + 共享 volume | 挂载 `./app`；API `--reload`；worker 需 restart |
| **交付 Docker** | 单独生成的 compose（不在本仓库） | Postgres + MinIO | 无源码挂载；可水平扩展 |

| 组件 | lite 本机 | lite Docker | full |
|------|-----------|-------------|------|
| `api` | ✅ | ✅ | ✅ |
| `worker` | —（进程内解析） | ✅ | ✅ |
| Valkey | — | ✅ | ✅ |
| Postgres / MinIO | — | — | ✅ |