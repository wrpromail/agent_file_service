# 实现状态看板

> 最后更新：**2026-09-30**  
> 对照：[design.md](../design.md) · [product-principles.md](./product-principles.md) · [archive-expand.md](./archive-expand.md) · [processing-factory.md](./processing-factory.md)

状态图例：✅ 完成 · 🟡 进行中 · ⚪ 未开始

---

## 里程碑总览

| 里程碑 | 状态 | 说明 |
|--------|------|------|
| M1 骨架与基础设施 | ✅ | Python 3.14、Valkey、Docker |
| M2 上传与去重 | ✅ | 流式哈希、metadata 同步 |
| M3 租户隔离与鉴权 | ✅ | `test_auth` |
| M4 异步解析 | ✅ | MinerU 融合、Celery/sync |
| M5 内容拉取与分片 | ✅ | `GET /content` 过滤；**未就绪 200+ready=false**；`smart_mode` 去空 sheet |
| M6 生命周期与 GC | ✅ | DELETE、会话列表、API lifespan 定时 GC、**竞态安全** |
| M7 元数据搜索与跨租户克隆 | ✅ | `GET /files`、`POST /clone`，编排层共享原语 |
| **Phase1 P0–P2** | ✅ | content 裁剪、ocr、多 block、PATCH、batch、key↔tenant、配额、metrics、ETag、from-url、archive members、音视频 L0 |

---

## 测试

```bash
# 推荐（与 CI/本地 uv 一致）
uv run pytest -q
# 或
python3 -m pytest tests/ -v
```

最近全量（2026-09-30）：**257 passed，2 skipped，0 failed**（含 file_convert 兼容层）。

| 测试文件 | 覆盖 |
|----------|------|
| `test_local_storage.py` | lite 本地盘存储、bootstrap / SQLite 路径 |
| `test_health.py` | 健康检查 + 启动依赖探测 |
| `test_config_yaml.py` | config.yaml 加载与 auto_parse 扁平化 |
| `test_upload.py` | 上传、去重、响应字段 |
| `test_streaming_upload.py` | 大文件流式上传（spool 落盘） |
| `test_auth.py` | Bearer / 租户白名单 / key↔tenant / 隔离 |
| `test_parse.py` | 解析触发、force、profile（含 ocr） |
| `test_delete.py` / `test_sessions.py` / `test_gc.py` | 生命周期与会话列表 |
| `test_gc_race.py` | GC 与上传竞态 |
| `test_expires_at.py` | 引用 `expires_at` |
| `test_legacy_office.py` | 旧式 doc/ppt/xls → LibreOffice |
| `test_mineru_client.py` | MinerU 客户端与映射 |
| `test_factory.py` | 处理工厂路由 |
| `test_metadata_handlers.py` | 分类型元数据（含音视频 L0） |
| `test_markitdown_layout.py` | MarkItDown → Markdown、多 block |
| `test_smart_markdown.py` | Excel 空 sheet 智能过滤 |
| `test_spreadsheet_tables.py` | **profile=tables** 解析器 + API content/auto 入队 |
| `test_content_view.py` / `test_structure_blocks.py` | content 裁剪 / 结构块拆分 |
| `test_vision_deep.py` / `test_litellm_client.py` | 视觉 / LiteLLM |
| `test_vision_pages_layout.py` | **vision-pages** 正文识别：栅格化、多页并发拼接、工厂路由 |
| `test_media_auto_layout.py` | 图片/音视频默认 layout 策略 |
| `test_custom_parse_api.py` | custom-parse API |
| `test_auto_parse_policy.py` | 分类型 auto_parse 门槛 |
| `test_outbound_http.py` | 出站 HTTP 重试 |
| `test_archive_expand.py` | 压缩包展开 + members |
| `test_from_url.py` | from-url + SSRF |
| `test_batch_files.py` | batch-status / batch-content |
| `test_patch_reference.py` | PATCH 引用 |
| `test_etag.py` | status/content ETag / 304 |
| `test_quota.py` | 租户配额 429 |
| `test_metrics_endpoint.py` | `GET /metrics` |
| `test_valkey.py` | Valkey 配置字段 |
| `test_uc_auth.py` | UC HMAC 票 / MOCK 换票 / jti 互斥 / HTTP 闸门（不打真 UC） |
| `test_convert_compat.py` | **file_convert 兼容层**：鉴权、office→md 缓存、excel、md→pdf、抽文本、转图、cache/clear |

单元测试默认：SQLite 内存库 + `InMemoryStorageBackend`；需真实 Valkey/LiteLLM 的用例会 skip。

---

## 模块清单

| 路径 | 状态 | 说明 |
|------|------|------|
| `app/core/hashing.py` | ✅ | **流式 SHA-256 + `SpooledTemporaryFile`**（>1 MiB 落盘，避免大文件常驻内存） |
| `app/core/storage.py` | ✅ | S3/MinIO + 内存后端；`stream_object` 流式下载；`GC_PURGED_URI` 哨兵 |
| `app/parsers/factory.py` | ✅ | `create_processing_factory()`；**正文识别后端** `layout_parser_name`=`markitdown\|simple\|mineru\|vision-pages`（写入 profile=layout） |
| `app/parsers/classifiers.py` | ✅ | 文件 kind/subtype 分类（接受 file-like 或 bytes） |
| `app/parsers/metadata_handlers.py` | ✅ | **分类型元数据 v0.2**：PDF/Office/Spreadsheet(xls+xlsx)/Text/Image；见 [metadata-extraction.md](./metadata-extraction.md) |
| `app/parsers/markitdown_layout.py` | ✅ | **默认正文识别**：文件 → Markdown 字符串 |
| `app/parsers/document_rasterize.py` | ✅ | PDF/Office 页图栅格化（pypdfium2；Office→LibreOffice→PDF） |
| `app/parsers/vision_pages_layout.py` | ✅ | **vision-pages** 正文识别：页图 + 多模态（可并发）→ Markdown 拼接 |
| `app/parsers/spreadsheet_tables.py` | ✅ | **profile=tables**：xlsx/xls/csv → headers+rows JSON（`spreadsheet_tables@v1`） |
| `app/parsers/smart_markdown.py` | ✅ | **智能模式**：Excel/CSV 读路径丢弃无内容 sheet |
| `app/integrations/mineru/` | ✅ | MinerU SaaS 客户端与映射（`full.md` → `metadata.markdown`；OSS PUT 无 Content-Type） |
| `app/integrations/litellm/` | ✅ | LiteLLM 视觉 / 音频 / 视频客户端（线程本地 HTTP client，支持页级并发） |
| `app/parsers/vision_deep.py` | ✅ | 图片 layout/deep（同源视觉） |
| `app/parsers/audio_layout.py` / `video_layout.py` | ✅ | 音视频 layout |
| `app/parsers/legacy_office.py` | ✅ | doc/ppt/xls → LibreOffice → OOXML |
| `app/core/dependency_health.py` | ✅ | API 启动依赖探测（LiteLLM/DB/Valkey/MinerU） |
| `app/services/dedup.py` | ✅ | **GC 竞态安全**：purging 等待 + purge 后 reuse 重传 |
| `app/services/file_service.py` | ✅ | 上传编排、去重、引用创建；**`list_files`**；**`clone_reference`**；**压缩包展开** |
| `app/services/archive_service.py` | ✅ | zip/tar 列表过滤、密码、too_many / expand 决策 |
| `app/services/lifecycle_service.py` | ✅ | 软删、**原子 GC claim**（`gc_state`）、会话列表 |
| `app/tasks/gc_tasks.py` | ✅ | GC 任务函数（由 **API lifespan** 定时调用，非 Celery Beat） |
| `app/tasks/parse_tasks.py` | ✅ | 异步解析任务 |
| `app/parsers/content_view.py` | ✅ | 读路径 outline / max_chars / sheets |
| `app/parsers/structure_blocks.py` | ✅ | layout 多 block 拆分 |
| `app/services/quota_service.py` | ✅ | 租户配额 |
| `app/core/metrics.py` / `audit.py` | ✅ | Prometheus 计数 + 结构化审计 |
| `app/uc/` | ✅ | UC 工作台跳转：env 配置、HMAC 票、token-auth、进程内 jti、FastAPI 闸门 |
| `app/services/url_import_service.py` | ✅ | from-url + SSRF |
| `app/services/convert_service.py` | ✅ | **file_convert 兼容层**（同步转换，产物入库为引用） |
| `app/api/routes/convert.py` | ✅ | `/convert*` `/extract-text` `/generate-excel` `/cache/*` |
| `app/parsers/markdown_pdf.py` | ✅ | Markdown → HTML → LibreOffice PDF |
| `app/mcp_convert.py` | ✅ | `/mcp` Streamable HTTP（工具名对齐 file_convert） |
| `app/tasks/convert_tasks.py` | ✅ | Docker 下转换在 worker 执行，API 同步等待 |

### 变更（2026-09-30）

- **file_convert 兼容层**：`/convert-office`、`/convert`、`/extract-text`、`/convert-to-images`、`/generate-excel`、`/cache/*`、`/mcp` 迁入本服务；须 Bearer + `X-Tenant-Id`；内部走引用 + worker。

### 变更（2026-08-21）

- **去掉 Alembic**：库表未定稿，启动改为 SQLAlchemy `create_all`（只补缺失表）。删除 `alembic/`、`alembic.ini` 及依赖。
- **根目录只留本地 compose**：删除 `docker-compose.deploy.yml`；交付编排另生成。

### 变更（2026-08-20）

- **UC 统一工作台鉴权**：`?token=` / `workbench_token` → `GET {UC}/uc/api/login/token-auth` → 本地 HMAC 票 `X-UC-Session`。默认关闸；`FILE_SERVICE_UC_*` 优先，别名 `DASHBOARD_UC_*`。有效 `API_KEYS` 的机器调用豁免。前端 `/auth` + 全屏「需要统一登录」。测试：`test_uc_auth.py`（不打真 UC）。文档：[13_identity_and_access.md](./13_identity_and_access.md)。

### 变更（2026-07-24）

- **压缩包上传即解压**：zip / tar / tar.gz / tar.bz2；可识别成员 ≤ `archive_max_expand_files`（默认 8）时同步展开为多个子 `FileReference`（过滤隐藏、`__MACOSX`、不支持扩展名、嵌套包）；过多 `too_many` 只存父包。Form `archive_password`（ZipCrypto，不落库）。`.json` 纳入可识别类型（`text/json`）。实现：`archive_service.py` + `file_service` 接入。文档：[archive-expand.md](./archive-expand.md)；样例：`sample/*.zip|tar*`；测试：`test_archive_expand.py`。
- **本地 Docker 热更**：`docker-compose.yml` 挂载 `./app` + API `uvicorn --reload`。

### 变更（2026-07-15）

- **`smart_mode`（默认 true）**：`GET /v1/files/{id}/content` 增加查询参数。对 Excel/CSV 等表格 Markdown 丢弃无实质内容的空 sheet（如默认空白 Sheet2/Sheet3），降低智能体上下文噪音。解析缓存仍存 MarkItDown 原始全文；`smart_mode=false` 可取回全量。实现：`app/parsers/smart_markdown.py`；测试：`test_smart_markdown.py`。文档：`design.md`、`agent-integration.md`、`processing-factory.md`、`product-principles.md`、`architecture-notes.md`、`configuration.md`、根 `README.md`。
- **服务能力文档：图片 / PPTX / 音频边界**：在 [product-principles.md](./product-principles.md) 增加「上传与识别能力矩阵」；同步相关文档。结论：上传无 MIME 白名单；图片一等。**后续**已补齐音视频 layout、图片默认视觉 layout、custom-parse（见 2026-07-16 变更日志）。

### 变更（2026-07-14）

- **产品原则文档化**：新增 [product-principles.md](./product-principles.md)（定位两层同心圆、保证线×能力菜单、结构/业务切分、Agent 契约）；同步更新 `design.md`、`agent-integration.md`、`architecture-notes.md`、`processing-factory.md`、`docs/README.md`、根 `README.md`。
- **分类型元数据提取 v0.2**：补齐 PDF（页数/加密/文档属性/文本层）、Spreadsheet（xlsx+xls sheet 名与维度）、Office/docx（段落/表格/标题预览）、Text（编码/CSV/Markdown 提示）、Image（宽高/格式）；注册 `office/*` 工厂路由。文档：[metadata-extraction.md](./metadata-extraction.md)。
- **`GET /v1/files`**：按元数据过滤查询引用（`session_id`、`agent_id`、`uploaded_by`、`filename` 模糊匹配、`content_hash`、分页），租户隔离不变。新增 `FileListItem` / `FileListResponse` schema。
- **`POST /v1/files/{id}/clone`**：引用克隆原语。以源 tenant credential 验证所有权，在目标 `target_tenant_id` 下创建新 `FileReference`，与源 reference 共享同一 blob 及解析结果，生命周期完全独立。新增 `CloneRequest` / `CloneResponse` schema。
- 架构决策：**跨智能体文件访问权限在 Agent Runtime / A2A 编排层处理**，file service 只提供所有权验证 + 引用克隆两个基础原语。

### 变更（2026-07-13）

- **大文件流式**：上传经 `SpooledTemporaryFile`（>1 MiB 落盘），解析经 `stream_object` 直写临时文件，全链路不再把整个文件读入内存为单一 `bytes`。
- **GC 与上传竞态安全**：`blobs` 新增 `gc_state`（`idle`/`purging`）列（迁移 `002`）。GC 原子 claim（条件 `UPDATE ... WHERE ref_count=0 AND gc_state='idle'`）；上传命中 purging 行时等待落定，命中 `GC_PURGED_URI` 行时重传 blob 字节并恢复真实 `storage_uri`。
- 元数据提取器契约由 `file_data: bytes` 改为 `file_data: BinaryIO`（seekable），分类器同步接受 file-like 或 bytes。
- 新增测试：`test_streaming_upload.py`（2）、`test_gc_race.py`（3）。
### 变更（2026-07-10）

- 默认 `LAYOUT_PARSER_NAME=markitdown`：上传后 layout 结果为 Markdown，供智能体直接注入上下文
- 智能体推荐读取 `content.metadata.markdown`（与 `blocks[0].text` 相同全文）
- 暂不做关系表 `[{object}]` 抽取；Excel 以 GFM Markdown 表输出

---

## API 端点

| 方法 | 路径 | 状态 |
|------|------|------|
| GET | `/health` | ✅ 存活 + 依赖状态（`?deep=true` 重探测） |
| GET | `/metrics` | ✅ Prometheus text（免鉴权，仅内网 scrape） |
| GET | `/v1/files` | ✅ 按元数据过滤、分页 |
| POST | `/v1/files` | ✅ 含 `expires_at`、`auto_parse`、**流式上传**、**archive 展开 / archive_password** |
| POST | `/v1/files/from-url` | ✅ SSRF 防护的 URL 拉取入库 |
| POST | `/v1/files/batch-status` | ✅ 多附件状态（逐项错误） |
| POST | `/v1/files/batch-content` | ✅ 多附件 content + 裁剪参数 |
| GET | `/v1/files/{id}` | ✅ |
| PATCH | `/v1/files/{id}` | ✅ 更新 session/agent/metadata/expires_at |
| DELETE | `/v1/files/{id}` | ✅ |
| POST | `/v1/files/{id}/clone` | ✅ 引用克隆，跨 tenant 共享 |
| GET | `/v1/files/{id}/status` | ✅ ETag / If-None-Match → 304 |
| GET | `/v1/files/{id}/content` | ✅ ready envelope + smart_mode + **mode/max_chars/offset/sheets** + ETag；profile 含 **tables** |
| GET | `/v1/files/{id}/download` | ✅ 原始 blob |
| POST | `/v1/files/{id}/parse` | ✅ `layout` / **`ocr`** / `deep` / **`tables`** / `force` |
| POST/GET | `/v1/files/{id}/custom-parse` | ✅ 图/音/视频用户指令（独立 job 表） |
| GET | `/v1/files/{id}/custom-parse/{job_id}` | ✅ 单次 custom-parse 结果 |
| GET | `/v1/files/{id}/archive/members` | ✅ 父包列目录（too_many 可探查） |
| GET | `/v1/sessions/{id}/files` | ✅ |

---

## GC 策略

- `ref_count == 0` 时清理对象存储（blob + parse 结果 JSON）
- 仍有软删 reference 时保留 blob 行，`storage_uri = gc://purged`，`gc_state = idle`
- 无任何 reference 时删除 blob 行
- **竞态安全**：GC 以条件 `UPDATE` 原子 claim（`idle→purging`），并发上传命中 purging 行时等待落定；命中已 purge 行（`gc://purged`）时重传 blob 字节

---

## 技术栈（实际）

| 组件 | 选型 |
|------|------|
| Python | **3.14**（`requires-python >= 3.14`） |
| 依赖 | **不锁版本**，`pip install` 取最新 |
| Web | FastAPI |
| ORM | SQLAlchemy 2（`create_all`；库表未定稿，无 Alembic） |
| 任务 | Docker：Celery worker；本机：BackgroundTasks；API lifespan GC |
| 元数据 | **lite：SQLite** / full：PostgreSQL |
| 对象存储 | **lite：本地文件系统** / full：MinIO/S3 |
| 解析 | ProcessingFactory → 正文识别（MarkItDown / MinerU / Simple / **vision-pages**）+ Vision / Audio / Video / tables |
| 部署 | 双镜像 api + worker；本仓库仅本地 `docker-compose.yml`；配置 `config.yaml` + 密钥 env |

---

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-08-21 | **去掉 Alembic**（`create_all`）；**删除交付 compose**，本仓库仅 `docker-compose.yml` 本地 lite |
| 2026-08-07 | **vision-pages 正文识别后端**：PDF/Office 转页图 + LiteLLM 多模态（默认 `kimi-k2.6:cloud`）→ Markdown；**`vision_pages_concurrency`** 多页并行后按页序拼接；`layout_parser_name` 语义澄清为正文识别后端（profile=layout 历史槽位）；MinerU `full.md`→`metadata.markdown`；修复 MinerU OSS PUT `Content-Type` 导致 SignatureDoesNotMatch；对比脚本 `scripts/compare_body_engines.py` |
| 2026-08-03 | **文档/用例对齐 `profile=tables`**：权威 docs 补全 SUPPORTED 含 tables、`also_tables`；修正 README 音视频 L0 与 API 速览；design 示例 `table_extraction`→`tables`；补 API/auto 用例；全量 **203 passed** |
| 2026-07-28 | **文档对齐**：`processing-factory` / `metadata-extraction` / `architecture-notes` 与图片 Vision layout、office/pptx、音视频 L0 实现一致；测试清单与 195 passed 对齐 |
| 2026-07-27 | **Phase1 P0–P2**：content 读路径裁剪（`mode`/`max_chars`/`offset`/`sheets`）；`profile=ocr` 正式支持；layout 多 block（Excel sheet / 标题/页，parser 0.2.0）；`PATCH` 引用；`batch-status`/`batch-content`；`API_KEY_TENANT_MAP` + audit 日志；租户配额 429；`GET /metrics`；status/content ETag；`POST /from-url`（SSRF）；`GET .../archive/members`；音视频 L0 元数据（duration 等） |
| 2026-07-16 | **启动依赖健康检查**：API lifespan 探测 DB / LiteLLM（含模型列表）/ Celery 时 Valkey / MinerU；`GET /health` 返回 `ok`/`degraded`；`config.yaml` 可关检查或 strict 拒启 |
| 2026-07-16 | **文档对齐**：图片默认 auto_parse=`layout`（视觉引擎）；音视频 layout 与 custom-parse 已产品化；GC 为 API lifespan 非 Celery Beat |
| 2026-07-16 | **仓库精简**：去掉空 `tests/test_parsers`、无用 Celery Beat schedule、`build.sh --full` 死路径；归档 implementation-plan |
| 2026-07-16 | **build.sh Trivy 缓存**：api/worker 均扫描；`TRIVY_CACHE_DIR`（默认 `~/.cache/trivy`）+ docker 挂卷；`--scanners vuln`；`TRIVY_SKIP_DB_UPDATE`（对齐 dev-security-standard §5.6） |
| 2026-07-16 | **build.sh 双镜像契约**：`last_image_tag.txt` 改为可 source 的 `IMAGE_TAG`/`API_IMAGE`/`WORKER_IMAGE`；构建后打 `:local`；`--skip-scan` |
| 2026-07-16 | **双镜像**：`api`（slim）+ `worker`（LibreOffice）；共享 `/data`；`PARSE_TASK_BACKEND=celery` |
| 2026-07-30 | Compose 拆为两套独立文件：`docker-compose.yml` 本地 lite+热更；`docker-compose.deploy.yml` 交付 full（移除 full 叠加） |
| 2026-07-15 | **默认 lite 模式**：SQLite + `LocalFilesystemStorageBackend`；full 曾用叠加文件（已废弃） |
| 2026-06-29 | M1–M3 完成 |
| 2026-06-29 | M4–M5：MinerU 融合、内容拉取 |
| 2026-06-29 | M6：DELETE、GC、会话列表 |
| 2026-06-29 | Celery Beat、`expires_at` 上传字段 |
| 2026-06-29 | Python 3.14、Valkey、ProcessingFactory；36 tests |
| 2026-06-30 | `GET /download`、图片 `deep`（qwen-vision）、MCP 工具封装说明；45 tests |
| 2026-07-14 | `GET /v1/files`（元数据过滤、分页）；`POST /v1/clone`（跨 tenant 引用克隆）；确立跨智能体共享架构决策 |
| 2026-07-13 | **大文件流式上传/解析**；**GC 与上传竞态安全**（`gc_state` 列，迁移 `002`）；元数据提取器改 file-like；+5 tests → 55 |

---

## 智能体集成

REST 与 MCP 说明：[agent-integration.md](./agent-integration.md)

---

## 待办（可选）

| 项 | 状态 |
|----|------|
| MinerU 真实环境 CI 测试 | ⚪ 需 `MINERU_API_TOKEN` |
| 压缩包按成员异步选择性展开 | ⚪ Phase1 仅列目录 |
| 浏览器预签名直传 | ⚪ 优先 from-url |