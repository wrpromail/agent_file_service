
# 文件智能处理服务 — 初始框架设计与实现文档

## 1. 服务定位与设计原则

本服务是多智能体系统中的一个**横切基础设施**,负责接收用户上传的文件,提取其事实性内容(元数据、OCR、结构化内容),并避免相同文件的重复处理。它服务于所有智能体,但本身不属于任何智能体的业务。

**产品形态（两层同心圆）**：

1. **文件管理底座** — 上传/下载、内容哈希去重、租户引用隔离、生命周期与 GC、引用克隆。  
2. **文档事实层** — 按类型的元数据提取、layout/OCR、（后续）结构切分与统一 schema。  

两者缺一不可：只有管理则各 Agent 重复解析；只有智能而无去重/引用则无法成为共享基础设施。

**处理触发模型（保证线 × 能力菜单）**：不走「上传后后台狂解析」，也不走「一切全靠 Agent 记得触发」。服务**保证**上传即有元数据、默认后台备好基础正文（layout）；**昂贵/可选** profile（deep、高精度引擎、force 重解析等）由智能体通过工具按需调用。完整契约见 [docs/product-principles.md](./docs/product-principles.md)；元数据字段见 [docs/metadata-extraction.md](./docs/metadata-extraction.md)。

构建与维护本服务时,以下原则具有最高优先级,任何实现细节不得违背:

**原则一:只供给事实,不做业务解读。** 服务回答"这个文件是什么、里面有什么、之前处理过没有"。它不回答"这意味着什么、该怎么用、要不要存进知识库"。后者属于智能体。

**原则二:统一格式,而非统一答案。** 不同解析工具(OCR 引擎、版面解析器)有各自的误差与倾向。服务用统一的 schema 交付结果,但**完整保留每一处判断的置信度、来源,乃至分歧**,绝不在底层把分歧消化成一个"标准答案"。采信与否是消费者的决定。

**原则三:物理文件去重,引用层隔离。** 相同内容(同一哈希)的文件全局只存一份、只解析一次。但每个租户/会话对文件的引用严格隔离——blob 可共享,reference 必须隔离。

**原则四:渐进式处理,成本可控。** 上传即返回廉价元数据;昂贵的 OCR/结构化异步进行;深度分析按需触发。绝不在上传环节同步执行高成本操作。

**原则五:无状态,不感知订阅方。** 服务只暴露状态查询接口,不主动回调通知"谁在等我"。事件分发交给外部消息层或编排框架。

**原则六:交付句柄,而非搬运内容。** 处理结果以稳定句柄(基于内容哈希)返回,消费者按需拉取所需部分(如"只要第 3-5 页"或"只要所有表格"),避免大块数据在系统中无意义搬运。

### 明确的边界外(Out of Scope)

以下内容**不**属于本服务,coding agent 不应实现,以防服务臃肿:

- 文件内容的业务语义关系建模(由智能体构建)
- 向量化、RAG 索引、知识图谱(由智能体或专门的数据层负责)
- 智能体的链路追踪与上报(由智能体侧负责,服务只返回轻量句柄供其记录)
- 带业务意图的深度分析(如"合规审核""报销校验")
- 主动推送/回调通知机制
- **业务语义切分**(如「招标商务卷 vs 技术卷」)；仅允许结构切分(页/标题树/表格区),见元数据文档第 7 节

---

## 2. 整体架构

服务在概念上分为三层,自下而上:

**存储与去重层(Blob Layer)。** 以内容哈希(SHA-256)为主键管理物理文件。负责实际的二进制存储(对象存储)、去重判断、引用计数与垃圾回收。这一层完全无业务、无租户概念。

**引用与会话层(Reference Layer)。** 管理"某租户/某会话/某智能体对某个 blob 的引用"。每个引用是一个稳定句柄(UUID),带有调用方提供的元数据、归属信息、生命周期(过期时间)。租户隔离在这一层强制实施。

**处理与解析层(Processing Layer)。** 一个可插拔的解析器框架,执行三阶段处理。解析结果以 `(content_hash, parser, version, profile)` 为键缓存,实现跨会话、跨租户的解析复用。

```
┌─────────────────────────────────────────────┐
│              API 层 (FastAPI)                 │
│   上传 / 查询元数据 / 拉取内容 / 触发解析       │
└───────────────┬───────────────────────────────┘
                │
   ┌────────────┼─────────────┐
   ▼            ▼             ▼
┌────────┐ ┌──────────┐ ┌──────────────┐
│Reference│ │Processing│ │  异步任务队列  │
│  Layer  │ │  Layer   │ │ (Celery+Redis)│
└────┬────┘ └────┬─────┘ └──────┬───────┘
     │           │              │
     ▼           ▼              ▼
┌─────────────────────────────────────────┐
│         Blob Layer (内容哈希去重)          │
│   PostgreSQL (元数据) + 对象存储 (二进制)   │
└─────────────────────────────────────────┘
```

---

## 3. 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.14 | 解析生态最丰富 |
| Web 框架 | FastAPI | 异步、类型安全（生产关闭 `/docs` 与 `/openapi.json`） |
| 异步任务 | Celery + Redis | 成熟的异步任务方案,适合长耗时解析 |
| 元数据库 | PostgreSQL 18 | 关系 + JSONB,适合混合结构 |
| ORM / 迁移 | SQLAlchemy 2.0（`create_all`） | 库表未定稿，暂不迁库 |
| 对象存储 | S3 兼容(生产)/ MinIO(本地) | 二进制与大体积解析结果存储 |
| 数据校验 | Pydantic v2 | 与 FastAPI 集成 |
| 容器化 | Docker + docker-compose | 本地一键启动全套依赖 |

初始解析依赖(可后续扩展):`pypdf` / `pdfplumber`(PDF)、`python-docx` / `openpyxl`(Office)、`Pillow` + `pytesseract` 或可配置的外部 OCR(图片)。**解析器必须设计成可插拔,具体引擎可替换。**

---

## 4. 数据模型

使用 SQLAlchemy 定义,以下为字段规格。

### 4.1 `blobs`(物理文件)

```python
class Blob:
    content_hash: str        # PK, SHA-256 十六进制字符串
    size_bytes: int
    mime_type: str
    storage_uri: str         # 对象存储中的位置, e.g. "blobs/ab/cd/abcd..."
    ref_count: int           # 引用计数, 归零后可被 GC
    created_at: datetime
```

去重核心:上传时先算哈希,若 `blobs` 已存在该哈希,则不重复存储二进制,仅 `ref_count += 1`。

### 4.2 `file_references`(引用 / 句柄)

```python
class FileReference:
    id: UUID                 # PK, 这是对外暴露的"句柄"
    content_hash: str        # FK -> blobs.content_hash
    tenant_id: str           # 租户隔离的关键字段, 必填
    session_id: str | None   # 会话归属
    agent_id: str | None     # 哪个智能体引用的
    original_filename: str
    uploaded_by: str | None
    custom_metadata: dict     # JSONB, 调用方自定义元数据, 服务不解读
    created_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None  # 软删除
```

**所有对外接口必须以 `reference_id` + `tenant_id` 为入口,严禁让调用方直接通过 `content_hash` 访问内容**——否则会击穿租户隔离(B 用户猜中哈希就能读到 A 的文件)。

### 4.3 `parse_results`(解析结果)

```python
class ParseResult:
    id: UUID                 # PK
    content_hash: str        # FK -> blobs.content_hash, 缓存的键之一
    parser_name: str         # e.g. "pdf_layout", "image_ocr"
    parser_version: str      # 解析器版本, 版本变化时结果失效
    profile: str             # "metadata" | "ocr" | "layout" | ...
    status: str              # "pending"|"processing"|"completed"|"failed"
    summary: dict            # JSONB, 小体积内联摘要(页数、块数等)
    result_uri: str | None   # 完整结构化内容存对象存储(可能很大)
    overall_confidence: float | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None

    # 唯一约束: (content_hash, parser_name, parser_version, profile)
```

缓存逻辑:触发某 profile 解析前,先查 `parse_results` 是否已有相同 `(content_hash, parser_name, parser_version, profile)` 且 `status=completed` 的记录。命中则直接复用,**不重复消耗算力**。这是跨会话复用解析结果的核心。

---

## 5. 统一结构化内容格式(中立中间表示)

这是原则二的落地。所有解析器无论底层用什么引擎,都必须输出这一 schema。完整内容存对象存储(`result_uri`),其精简摘要存 `summary` 字段。

```json
{
  "schema_version": "1.0",
  "content_hash": "abcd1234...",
  "source": {
    "parser_name": "pdf_layout",
    "parser_version": "0.1.0",
    "profile": "layout"
  },
  "document": {
    "page_count": 12,
    "language": ["zh", "en"]
  },
  "blocks": [
    {
      "id": "b-001",
      "type": "heading | paragraph | table | image | list | footer",
      "page": 1,
      "bbox": [72.0, 100.5, 520.0, 130.0],
      "text": "合同标的",
      "confidence": 0.98,
      "metadata": {}
    }
  ],
  "tables": [
    {
      "id": "t-001",
      "page": 3,
      "bbox": [...],
      "rows": [["项目", "金额"], ["服务费", "10000"]],
      "confidence": 0.91,
      "cell_confidences": [[0.99, 0.99], [0.95, 0.72]]
    }
  ],
  "warnings": [
    {
      "block_id": "b-045",
      "type": "ocr_low_confidence | engine_disagreement",
      "detail": "两个 OCR 引擎对该区域结果不一致",
      "candidates": ["100O0", "10000"]
    }
  ]
}
```

注意 `confidence`、`cell_confidences`、`warnings` 与 `candidates` 字段:**分歧和不确定性是一等公民,如实暴露给消费者,由其按业务风险决定是否复核,服务绝不替它收敛。**

---

## 6. 三阶段处理流程

与产品契约对齐：**阶段一、二 ≈ 保证线**；**阶段三 ≈ 能力菜单**。详见 [docs/product-principles.md](./docs/product-principles.md)。

**阶段一 — 元数据提取(同步,毫秒级,上传即返回) — 保证线 L0。**
计算哈希 → 去重检查 → 按文件类型提取廉价元数据(MIME、大小、PDF 页数、Excel sheet 数、docx 段落/表格提示等)→ 创建 reference → 立即返回句柄与基础元数据。此阶段不做 OCR。失败时降级为 MIME+size,不得阻断上传。**上传默认不设 MIME 白名单**(图片 / PPTX / 音频等均可入库);专用元数据仅覆盖已实现的 kind。

**阶段二 — 结构化提取(异步,可缓存) — 保证线 L1。**
上传后**默认**自动入队(`auto_parse=true`; 可由请求参数关闭)。Celery worker 按分类执行 layout(及扫描件等默认的 ocr),产出第 5 节的统一格式(默认 Markdown),存对象存储,更新 `parse_results.status`。命中缓存则跳过。**不是**「后台持续跑所有可能的解析」。layout 对类型的覆盖见下「格式能力」与 [docs/product-principles.md](./docs/product-principles.md)。

**阶段三 — 按需深度处理(显式触发) — 能力菜单。**
通过 `POST /v1/files/{id}/parse` 指定 profile 触发(如 `deep`、force 重跑)。**仅限与业务无关的通用能力**(图表理解、高精度版面等)。带业务意图的分析不在此实现。智能体通过 REST/MCP 工具发现并调用;服务不猜测业务意图。

调用方通过轮询 `GET .../status` 获知阶段二/三进度,**服务不主动回调**。

### 6.1 对智能体的承诺(摘要)

| | 内容 |
|--|------|
| 保证 | metadata 上传即有; layout 默认后台准备,可查 status |
| 不保证 | deep 等昂贵 profile 在无人请求时已完成; 依赖外部模型的 layout 在未配置时可能失败 |
| Agent 负责 | 按需 trigger; 业务解读与字段抽取 |

### 6.1.1 格式能力(上传 vs 识别,摘要)

| 类型 | 上传 | L0 元数据 | layout Markdown | 备注 |
|------|------|-----------|-----------------|------|
| 常见图片 | ✅ | ✅ image | ✅ | deep 视觉按需 |
| `.pptx` / `.docx` / 表格 | ✅ | ✅ office / spreadsheet | ✅ MarkItDown | — |
| 旧 `.ppt` / `.doc` / `.xls` | ✅ | ✅ | ✅ LibreOffice→OOXML 后 layout | 见 [docs/legacy-office.md](./docs/legacy-office.md)；需 worker |
| 音频 / 视频 | ✅ | audio / video | ⚠️ 依赖 LiteLLM 多模态 | 未配置模型时 layout 可能失败 |

权威矩阵见 [docs/product-principles.md](./docs/product-principles.md)。

### 6.2 文档划分边界

- **允许(结构切分)**: 页、标题树、段落/表格块、稳定 block id — 属 layout/事实层。  
- **禁止(业务切分)**: 「商务卷/技术卷」、甲方金额等业务字段 — 属智能体。

---

## 7. API 设计

所有接口需通过鉴权中间件解析出 `tenant_id`(从 API Key 或 JWT),并在每次数据访问时校验 reference 归属。

### 上传文件
```
POST /v1/files
Content-Type: multipart/form-data
Headers: Authorization, X-Tenant-Id

Form fields:
  file:        (binary)        必填
  session_id:  string          可选
  agent_id:    string          可选
  auto_parse:  bool=true       是否自动触发阶段二
  metadata:    json string     调用方自定义元数据

Response 201:
{
  "reference_id": "uuid",
  "content_hash": "abcd...",
  "deduplicated": true,         // 该内容此前已存在
  "metadata": {                 // 阶段一同步结果
    "filename": "contract.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 204800,
    "page_count": 12
  },
  "parse_status": "pending"     // 阶段二状态
}
```

### 查询引用信息与解析状态
```
GET /v1/files/{reference_id}
GET /v1/files/{reference_id}/status?profile=layout

Response 200:
{
  "reference_id": "uuid",
  "metadata": {...},
  "parse_results": [
    {"profile": "metadata", "status": "completed"},
    {"profile": "layout",   "status": "processing"}
  ]
}
```

### 按需拉取结构化内容(支持分片)
```
GET /v1/files/{reference_id}/content
    ?profile=layout
    &pages=3-5            // 可选, 页范围
    &types=table         // 可选, 只要某类 block
    &include_confidence=true
    &smart_mode=true      // 默认 true; 智能体友好后处理

Response 200: 第 5 节格式(按 query 过滤后的子集)
若解析未完成 → **200** + `ready=false`，body 含 `status` / `message` / `message_code`（方案 B，不用 409 表示未就绪）
```

**`smart_mode`（默认 `true`）**：面向智能体的只读后处理，**不改写** `parse_results` 缓存中的原始产物。

| 开关 | 行为 |
|------|------|
| `true`（默认） | 对 Excel/CSV 等表格 Markdown：丢弃无实质单元格内容的 `## Sheet*` 段（如工作簿默认空白 Sheet2/Sheet3），避免噪音进入 Agent 上下文 |
| `false` | 返回 MarkItDown 等引擎的完整原始 Markdown（含空 sheet） |

响应 `metadata` 可含：`smart_mode`、`smart_dropped_sheets`（丢弃的 sheet 数，若有）。实现见 `app/parsers/smart_markdown.py`；产品约定见 [docs/agent-integration.md](./docs/agent-integration.md)、[docs/processing-factory.md](./docs/processing-factory.md)。

### 触发深度解析
```
POST /v1/files/{reference_id}/parse
Body: { "profile": "tables", "force": false }
Response 202: { "profile": "...", "status": "pending" }
```

### 列举会话内文件
```
GET /v1/sessions/{session_id}/files
Response 200: { "files": [ {reference 摘要}, ... ] }
```

### 删除引用(软删 + 引用计数递减)
```
DELETE /v1/files/{reference_id}
Response 204
// 仅删除 reference; blob 在 ref_count 归零后由 GC 任务清理
```

---

## 8. 可插拔解析器框架

解析器是本服务最需要保持开放扩展的部分。定义统一接口,新引擎只需实现接口并注册。

```python
# app/parsers/base.py
from abc import ABC, abstractmethod

class Parser(ABC):
    name: str
    version: str
    supported_mime_types: list[str]
    profile: str

    @abstractmethod
    def can_handle(self, mime_type: str) -> bool: ...

    @abstractmethod
    def parse(self, file_path: str, context: ParseContext) -> StructuredContent:
        """返回第 5 节定义的统一格式。
        必须填充 confidence;遇到不确定/分歧时写入 warnings,
        严禁自行收敛为单一答案。"""
        ...
```

```python
# app/parsers/registry.py
class ParserRegistry:
    def register(self, parser: Parser): ...
    def get_parser(self, mime_type: str, profile: str) -> Parser: ...
```

初始需实现的解析器:`MetadataParser`(阶段一,全类型)、`PdfLayoutParser`、`OfficeParser`、`ImageOcrParser`。

---

## 9. 项目结构

```
file-service/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # Pydantic Settings, 环境变量
│   ├── api/
│   │   ├── deps.py             # 鉴权、tenant 解析、依赖注入
│   │   └── routes/
│   │       ├── files.py
│   │       └── sessions.py
│   ├── core/
│   │   ├── hashing.py          # 流式 SHA-256
│   │   ├── storage.py          # 对象存储抽象 (S3/MinIO)
│   │   └── security.py         # 租户隔离校验
│   ├── db/
│   │   ├── session.py
│   │   └── base.py
│   ├── models/                 # SQLAlchemy: blob, reference, parse_result
│   ├── schemas/                # Pydantic 请求/响应模型
│   ├── services/
│   │   ├── file_service.py     # 上传编排、去重、引用创建
│   │   ├── dedup.py
│   │   └── parse_service.py    # 解析触发、缓存查询
│   ├── parsers/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── metadata.py
│   │   ├── pdf.py
│   │   ├── office.py
│   │   └── image_ocr.py
│   └── tasks/
│       ├── celery_app.py
│       ├── parse_tasks.py      # 异步解析任务
│       └── gc_tasks.py         # 引用归零的 blob 清理
├── tests/                      # pytest（见 docs/implementation-status.md）
├── docker-compose.yml          # 本地：lite + 源码挂载 + reload（交付 compose 另生成）
├── Dockerfile                  # multi-target: api | worker
├── build.sh
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 10. 给 Coding Agent 的实现里程碑

按以下顺序增量实现,每个里程碑结束应可运行且有测试覆盖。

**M1 — 骨架与基础设施。** 搭建 FastAPI 应用、配置加载、docker-compose（本地 lite）、数据库模型与 SQLAlchemy `create_all`、对象存储抽象、健康检查接口。

**M2 — 上传与去重(阶段一)。** 实现流式哈希、blob 去重存储、reference 创建、`MetadataParser`、`POST /v1/files` 与 `GET /v1/files/{id}`。**重点:`deduplicated` 标志正确,ref_count 正确递增。**

**M3 — 租户隔离与鉴权。** 实现鉴权中间件、`tenant_id` 解析、每次访问的归属校验。**`test_isolation.py` 必须覆盖"B 租户无法通过任何手段访问 A 的 reference"。** 这是安全红线,不可推迟。

**M4 — 异步解析(阶段二)。** 接入 Celery,实现 `PdfLayoutParser` 与 `ImageOcrParser`,产出第 5 节统一格式(含 confidence 与 warnings),`parse_results` 缓存命中逻辑,状态查询接口。

**M5 — 内容拉取与分片。** 实现 `GET .../content` 的 profile / pages / types 过滤,大结果从对象存储流式返回。读路径支持 `smart_mode`（默认 true）：表格 Markdown 丢弃无内容 sheet,不改写解析缓存。

**M6 — 按需解析、生命周期与 GC。** 实现 `POST .../parse`、reference 过期清理、ref_count 归零的 blob 垃圾回收任务、`DELETE` 接口。

---

## 11. 实现中必须守住的几条红线

**租户隔离不可后补(M3 不可推迟)。** blob 层共享是性能优化,但任何内容访问必须先验证调用方持有指向该 blob 的合法 reference。去重不得成为越权读取的通道。

**解析器不得收敛分歧。** 任何解析器遇到低置信度或多引擎不一致时,必须写入 `warnings` 与 `candidates`,而非自行挑一个填进 `text`。这是原则二的代码级保证。

**服务不存储业务衍生数据。** 不要在本服务里加任何"知识库""向量""审核结论"相关的表或字段。一旦出现这类需求,说明边界被侵犯,应拒绝并交还智能体。

**不实现回调/推送。** 即使看起来方便,也只提供状态查询。保持服务对"谁在等结果"完全无知。

---

## 实现进展（文档索引）

本文档为设计源稿。落地实现、状态看板与架构差异说明见 [docs/](./docs/)：

| 文档 | 内容 |
|------|------|
| [docs/implementation-status.md](./docs/implementation-status.md) | 里程碑完成度、测试、变更日志 |
| [docs/processing-factory.md](./docs/processing-factory.md) | 类型/子类型/层级处理工厂 |
| [docs/configuration.md](./docs/configuration.md) | 环境变量（含 Valkey、MinerU、GC） |
| [docs/architecture-notes.md](./docs/architecture-notes.md) | 与本文的差异及技术取舍 |
| [docs/archive-expand.md](./docs/archive-expand.md) | 压缩包上传即解压（zip/tar、密码、children） |

**已实现差异摘要**：消息队列使用 Valkey；解析路由由 `ProcessingFactory` 按文件子类型选择元数据/ layout/ ocr 处理器；Python 3.14、依赖不锁版本；上传与解析全链路流式（`SpooledTemporaryFile` + `stream_object`，避免大文件常驻内存）；blob GC 以 `gc_state` 列与条件 `UPDATE` 实现与并发上传的竞态安全；**压缩包**（zip/tar）可识别成员 ≤N 时同步展开为子 reference（见 [docs/archive-expand.md](./docs/archive-expand.md)）。


