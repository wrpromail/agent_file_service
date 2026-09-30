# 架构实现笔记

本文档记录对 [design.md](../design.md) 的解读、已实现架构与技术取舍。实现状态见 [implementation-status.md](./implementation-status.md)。

产品契约（保证线 × 能力菜单、结构/业务切分）见 **[product-principles.md](./product-principles.md)**。

## 核心理解

服务是 **三层存储 + 分层处理管道**：

1. **Blob Layer** — SHA-256 内容寻址，全局去重
2. **Reference Layer** — UUID 句柄 + `tenant_id` 隔离
3. **Processing Layer** — `(content_hash, parser, version, profile)` 缓存解析产物

读路径：`tenant_id` → 校验 `FileReference` → `content_hash` → blob / parse_result。**禁止裸 `content_hash` API。**

### 产品形态与触发模型（架构含义）

| 产品层 | 架构落点 |
|--------|----------|
| 文件管理底座 | Blob + Reference + GC + clone API |
| 文档事实层 | ProcessingFactory + parse_results 缓存 |
| **保证线 L0** | 上传路径同步 `MetadataParser` / 分类型 extractors |
| **保证线 L1** | `auto_parse=true` → layout（及类型默认 ocr）；**spreadsheet 默认可并行 `tables`**（`also_tables`，非「跑满全部 profile」） |
| **能力菜单** | `POST /parse` / MCP `trigger_file_parse`（deep / ocr / tables 等） |

**刻意不做**：上传后无差别跑满全部 profile（deep 等仍按需）；服务端回调订阅方；业务语义解析。

### Phase1 增量（实现要点）

| 能力 | 落点 |
|------|------|
| content 裁剪 | `app/parsers/content_view.py` → `filter_structured_content` |
| 多 block | `app/parsers/structure_blocks.py`（markitdown/simple 0.2.0） |
| ocr profile | `SUPPORTED_PARSE_PROFILES` 含 ocr；工厂已注册 |
| tables profile | spreadsheet 结构化 JSON；`also_tables` 默认并行入队；`SpreadsheetTablesParser` |
| PATCH / batch / from-url | `files.py` + `url_import_service` / `update_reference` |
| key↔tenant | `API_KEY_TENANT_MAP` → `assert_key_tenant_allowed` |
| 配额 | `quota_service.py` → 429 |
| 指标 | `GET /metrics` + `core/metrics.py` |
| ETag | status/content `If-None-Match` |
| archive members | `list_archive_members` |
| 音视频 L0 | `AudioMetadataExtractor` / `VideoMetadataExtractor` |
| file_convert 兼容层 | `app/api/routes/convert.py` + `convert_service`；同步 HTTP，产物入库为 `FileReference`；Docker 下 Celery `.get()` 等 worker |

**上传 vs 识别**：API 上传层无 MIME 白名单；分类覆盖 pdf/image/office/spreadsheet/audio/video 等；旧式 Office 见 [legacy-office.md](./legacy-office.md)。格式矩阵见 [product-principles.md](./product-principles.md)。兼容转换路径见 [AGENT_API.md §5.8](../AGENT_API.md)。

## 处理工厂（当前架构核心）

在可插拔 `Parser` 之上增加 **ProcessingFactory**，解决「同 MIME、不同来源需不同策略」：

```text
上传文件
  → classify_file()             # kind + subtype（含 office/pptx、audio、video、archive）
  → resolve_metadata_extractor  # 保证线 L0：同步元数据
  → auto_parse? schedule…       # 保证线 L1：默认 layout/ocr；spreadsheet 可并行 tables
  → Agent POST /parse           # 能力菜单：deep / ocr / tables 等
```

详见 [processing-factory.md](./processing-factory.md) · [metadata-extraction.md](./metadata-extraction.md)。

```text
ProcessingFactory
  ├── classify: pdf/* | image/* | office/* | spreadsheet/* | text/* | audio/* | video/* | archive | unknown
  ├── metadata:  分类型 extractors | GenericMetadataExtractor
  └── parser:     正文识别后端 markitdown/simple/mineru/vision-pages（写入 profile=layout）；
                  spreadsheet tables → SpreadsheetTablesParser；
                  image layout/deep → VisionDeep；audio/video → LiteLLM layout parsers
```

缓存键使用**实际 parser 的 name + version**，子类型不同则缓存隔离。

## 大文件流式

上传与解析全链路避免把整个文件读入内存为单一 `bytes`：

| 阶段 | 实现 | 内存上界 |
|------|------|----------|
| 上传哈希 | `read_and_hash` 流式读 → `SpooledTemporaryFile`（>1 MiB 落盘） | 1 MiB chunk |
| 元数据提取 | 提取器契约改为 `BinaryIO`（seekable），按需读取头部/首页 | 取决于解析器 |
| Blob 存储 | `storage.put_object` 走 `upload_fileobj`（file-like 流式） | 1 MiB chunk |
| 解析执行 | `_download_blob_to_temp` 用 `storage.stream_object` 直写临时文件，parser 从路径读 | 1 MiB chunk |

`MetadataExtractor.extract` 与 `classify_file` 现在接受 seekable file-like **或** `bytes`（旧调用点传 `b""` 仍兼容，命中 DB 缓存分类时不读文件）。

## GC 与上传竞态

`blobs` 新增 `gc_state` 列（`idle` / `purging`，迁移 `002`）。竞态在 GC 与上传两侧都加了护栏：

```text
GC（gc_blob）：
  UPDATE blobs SET gc_state='purging'
    WHERE content_hash=? AND ref_count=0 AND gc_state='idle'
  -- rowcount=0 即放弃（被人改了 ref_count 或已在 purging）
  ...删除存储 + parse 结果...
  -- 清理后重检 ref_count：
  ref_count==0 → 删行
  ref_count>0  → 保留行，storage_uri=gc://purged，gc_state=idle

上传（store_or_reuse_blob）：
  SELECT ... FOR UPDATE           -- 串行化（Postgres）
  命中 purging 行 → 轮询等落定（≤2s）
  命中 gc://purged 行 → 重传 blob 字节，恢复真实 storage_uri
  ref_count += 1
```

不变量：**任何活跃 reference 的 `storage_uri` 永远指向 live 存储**。GC purge 后若新上传命中同 hash，必先重传再返回 reference，杜绝「引用指向已删存储」。

## 存储分层

| 数据 | lite（默认） | full |
|------|--------------|------|
| blob、reference、parse 状态 | **SQLite**（`./data/db` 或 `/data/db`） | PostgreSQL |
| 原始文件、StructuredContent | **本地文件系统** `LocalFilesystemStorageBackend`（`local://…`） | MinIO/S3（`s3://…`） |
| 任务队列 | Docker：Valkey + Celery；本机：`BackgroundTasks` | Valkey + Celery |
| 容器镜像 | **api**（slim）+ **worker**（LibreOffice）共享 `/data` | 同左 + 中间件 |

切换：`DEPLOY_MODE` / `DATABASE_URL` / `STORAGE_BACKEND`（见 [configuration.md](./configuration.md)）。

## MinerU 集成（正文识别后端之一）

- 客户端：`app/integrations/mineru/client.py`（官方 SaaS `mineru.net/api/v4`）
- 映射：`content_list.json` → blocks/tables；**`full.md` → `metadata.markdown`**
- OSS 上传：预签名 PUT **不带** `Content-Type`（避免 SignatureDoesNotMatch）
- 启用：`LAYOUT_PARSER_NAME=mineru`；测试：`simple`

## vision-pages 集成（正文识别：页图 + 多模态）

- 栅格化：`app/parsers/document_rasterize.py`（PDF→pypdfium2；Office→LibreOffice→PDF）
- 识别：`app/parsers/vision_pages_layout.py`；LiteLLM 模型默认 `kimi-k2.6:cloud`
- **多页并发**：`vision_pages_concurrency`（默认 3）；完成后按页号拼接 `<!-- page N -->`
- 启用：`LAYOUT_PARSER_NAME=vision-pages`；产物仍写入 **profile=layout**（基础正文槽）

## API 与 Service 分层

| 层 | 职责 |
|----|------|
| `routes/` | HTTP、与 `BackgroundTasks` 交互进行非阻塞后台调用 |
| `services/file_service.py` | 上传：流式 hash → dedup → metadata → reference |
| `services/dedup.py` | **竞态安全** blob 去重/重传（`gc_state` 协调） |
| `services/parse_service.py` | 工厂选 parser、后台执行调度、缓存、**流式下载到临时文件** |
| `services/lifecycle_service.py` | 软删、**原子 GC claim**、会话列表 |
| `core/security.py` | `get_owned_reference()`、`ensure_api_keys()`、tenant allowlist |

## 鉴权

- `Authorization: Bearer <api_key>` — **始终必填、不可关闭**；`API_KEYS` 多值逗号分隔；空则启动生成 ephemeral key 并打日志
- `X-Tenant-Id: <tenant>` — 必填；可选 `ALLOWED_TENANT_IDS` 白名单（含 clone target）
- **L2 UC 工作台跳转**（默认关）：开闸后控制台须从 `/auth?token=` 换本地 `X-UC-Session`；无票且无 L0 Bearer 的 `/v1` → 401。见 [13_identity_and_access.md](./13_identity_and_access.md)
- 跨租户访问统一 404；白名单外 tenant → 403
- FastAPI `/docs`、`/redoc`、`/openapi.json` 已关闭；开发控制台为静态 MPA（`web/`，`DEV_CONSOLE_ENABLED` 控制挂载；见 [frontend-dev-console.md](./frontend-dev-console.md)），业务 API 仍由网关/客户端注入 Bearer + `X-Tenant-Id`
- `GET /health`、`GET /metrics`、静态 `web/`、`/api/auth/uc/*` 免 UC 票（metrics / 控制面仅内网）

## 异步与定时任务

| 功能 | Docker（默认） | 本机 dev（`PARSE_TASK_BACKEND=local`） |
|------|----------------|----------------------------------------|
| 异步 layout / deep | API 入队 → **Celery worker** | FastAPI `BackgroundTasks` |
| 定时 GC | **API lifespan** 调 `gc_tasks`（不跑 Celery Beat） | 同左 |
| 旧式 Office 转换 | **仅 worker** 镜像有 LibreOffice | 本机需自备 `soffice`，或只用 Docker |

- `expire_references_task` / `run_blob_gc_task`：清理过期引用与 `ref_count=0` blob  
- `PARSE_SYNC=true`：解析在请求内同步执行（测试常用）  
- `gc_beat_enabled`：开关 **API 进程内** GC 循环，与 Celery Beat 无关

## 部署拓扑

### lite（默认 Docker：api + worker）

```text
┌─────────────────┐     Valkey/Celery     ┌──────────────────────────┐
│  api (:8020)    │ ───────────────────► │  worker                  │
│  · 上传/下载    │                      │  · layout / deep         │
│  · 入队         │                      │  · LibreOffice 旧 Office │
│  · lifespan GC  │                      │  · 后续重依赖            │
└────────┬────────┘                      └────────────┬─────────────┘
         │              volume: /data                 │
         └──────────────── shared ────────────────────┘
           SQLite · blobs · parse-results · derivatives
```

本机单进程开发：`PARSE_TASK_BACKEND=local`，不启 worker。

```bash
# 本地开发：compose 现场 build（target api / worker）
docker compose up -d --build

# 构建版本化镜像 + :local + Trivy
./build.sh --native
set -a && source last_image_tag.txt && set +a
docker compose up -d --force-recreate
```

交付编排不在本仓库。full（Postgres + MinIO）由交付流程单独生成 compose。

```text
┌─────────┐   Celery    ┌──────────┐
│  api    │ ──────────► │  worker  │
└────┬────┘             └────┬─────┘
     ├──────────┬────────────┼──────────┐
     ▼          ▼            ▼          ▼
  Postgres   Valkey       MinIO    (shared /data 临时)
```

### 镜像制品约定

| 项 | 值 |
|----|-----|
| 镜像名 | `agent-file-service-api` / `agent-file-service-worker` |
| 标准 tag | `{arch}-{timestamp}-{git7}`（两镜像共享 `IMAGE_TAG`） |
| 本地 tag | `:local`（`build.sh` 在 load 后额外打，对齐 compose 默认） |
| 制品文件 | `last_image_tag.txt`：`IMAGE_TAG=` / `API_IMAGE=` / `WORKER_IMAGE=`（可 `source`） |

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 租户隔离遗漏 | 统一 `get_owned_reference` |
| 大文件 OOM | **上传 SpooledTemporaryFile + 解析 stream_object 直写临时文件**，全链路 1 MiB chunk |
| GC 与上传竞态 | **`gc_state` 列 + 条件 UPDATE 原子 claim**；上传命中 purging 等待、命中 purged 重传 |
| 解析器升级脏缓存 | `parser_version` 纳入唯一键 |
| 软删后 blob FK | GC 保留 tombstone 行 `gc://purged`，`gc_state` 复位 idle |
| 子类型误判 | 分类结果写入 summary，可人工复核 |
| 压缩包 zip bomb / 过多成员 | 成员数上限 + 单成员/合计字节上限；过多只存父包 |
| zip 密码进日志/DB | 仅请求内存使用；禁止写入 metadata / custom_metadata |

## 压缩包展开（简）

上传路径在父 reference 入库后调用 `archive_service`：

1. 分类 `FileKind.ARCHIVE`  
2. 列成员并过滤 → 决策 `expanded | too_many | empty`  
3. `expanded` 时对每个成员 `_ingest_raw_bytes`（`expand_archives` 不再递归）  

父包跳过 auto layout。完整契约：[archive-expand.md](./archive-expand.md)。

## 跨租户引用克隆

跨智能体文件共享的核心不变量：**`reference_id` 是访问令牌**。持有 `(reference_id, tenant_id)` 即可访问文件；谁应当持有、如何传递，是编排层的职责。

file service 提供的具体原语：

```text
clone_reference（ source_reference, target_tenant_id ）：
  SELECT blob FOR UPDATE          -- 串行化，防止与 GC 竞态
  blob.ref_count += 1
  创建新 FileReference（
    content_hash = source.content_hash,
    tenant_id    = target_tenant_id,
    custom_metadata = {
      cloned_from:      source.id,
      source_tenant_id: source.tenant_id,
      ...调用方字段
    }
  ）
```

解析结果通过 `content_hash` 自动共享，不重复解析。两个 reference 生命周期完全独立。

## 与 design.md 的差异（有意识）

| design.md | 实际实现 |
|-----------|----------|
| Redis | **Valkey**（兼容） |
| Python 3.14 | ✅ 已采用 |
| 固定 ParserRegistry | **ProcessingFactory** 按子类型路由 |
| Celery + Beat 多容器 | **Docker：api + worker + Valkey**；GC 在 API lifespan（无 Beat 容器）；本机可 BackgroundTasks |
| 仅 Postgres + MinIO | 本仓库 compose 为 **lite**；交付 full 的 compose **另生成** |
| 单镜像 | **双镜像**：api 轻量 / worker 承载 LibreOffice 与后续重依赖 |
| layout 以版面 blocks/tables 为主 | **默认 MarkItDown → 单一 Markdown 字符串**（智能体从简）；`mineru`/`simple` 仍可选 |
| 业务结构化抽取 | **不做**；`[{...}]` 等由智能体框架侧处理 |
| blob GC 简单 ref_count 归零即删 | **`gc_state` 协调的原子 claim + purge 后 reuse 重传**（竞态安全） |
| 跨智能体权限设计 | **权限判断在 Agent Runtime / A2A 编排层**，file service 只提供所有权验证 + 引用克隆两个原语 |
| content 仅 profile/pages/types 过滤 | **增加 `smart_mode`（默认 true）**：表格读路径去空 sheet；缓存仍存原始 Markdown |

### 智能模式（`smart_mode`）取舍

| 决策 | 说明 |
|------|------|
| 过滤放在读路径而非解析写路径 | 缓存保留引擎全量结果，`smart_mode=false` 可还原；不增加缓存维度 |
| 默认开启 | 智能体主路径少噪音；空白 Sheet2/Sheet3 对模型只有干扰 |
| 仅表格 MIME/扩展名 | 避免 PDF/Office 中合法 `##` 标题被误删 |
| 不属于业务解读 | 只做「无实质单元格 → 丢弃 sheet 段」，不做字段抽取或语义判断 |

实现：`app/parsers/smart_markdown.py` · 挂载点 `filter_structured_content` / `GET /v1/files/{id}/content`。

`design.md` 仍为设计源文档；落地细节以本目录为准。
