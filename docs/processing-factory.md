# 处理工厂（ProcessingFactory）

本文档说明 `ProcessingFactory` 的设计与扩展方式。工厂按 **文件类型（kind）+ 子类型（subtype）+ 处理层级（level）** 路由到不同处理器，实现「同一文件格式、不同来源用不同策略」的需求。

产品层面对「谁自动跑、谁按需跑」的约定见 **[product-principles.md](./product-principles.md)**；元数据字段见 [metadata-extraction.md](./metadata-extraction.md)。

## 处理层级

| 层级 | `ProcessingLevel` | 同步/异步 | 产品角色 | 典型用途 |
|------|-------------------|-----------|----------|----------|
| 元数据 | `metadata` | 同步 | **保证线 L0** | 页数、sheet 数、尺寸；毫秒级返回 |
| 结构化 | `layout` | 异步（默认 auto） | **保证线 L1** | 版面、表格、段落块、Markdown |
| 精细识别 | `ocr` | 异步 | **能力菜单 / 扫描件默认可自动** | 扫描 PDF、document_scan 图；API `POST /parse?profile=ocr` 已正式支持 |
| 深度处理 | `deep` | 按需触发 | **能力菜单** | 视觉理解等业务无关通用能力 |
| 表格 JSON | `tables` | 异步 | **spreadsheet 保证线扩展**（`also_tables`，默认 on） | xlsx/xls/csv → headers+rows 结构化 JSON（与 layout Markdown 并行） |

**API 支持的 trigger / content profile**（`SUPPORTED_PARSE_PROFILES`）：`layout` · `ocr` · `deep` · **`tables`**。

## 文件分类

`classify_file(filename, mime_type, file_data)` → `FileClassification`：

| kind | 子类型示例 | 判定依据 |
|------|-----------|----------|
| `pdf` | `digital`, `scanned`, `unknown` | 首页文本层是否充足 |
| `image` | `photo`, `screenshot`, `document_scan` | MIME `image/*`；子类型看文件名关键词（scan/screenshot 等） |
| `spreadsheet` | `xlsx`, `xls`, **`et`**, **`ett`** | MIME / 扩展名（含金山 WPS 表格） |
| `office` | `docx`, `doc`, **`pptx`**, **`ppt`**, **`wps`/`wpt`**, **`dps`/`dpt`** | MIME / 扩展名（演示文稿与金山 WPS 文字/演示） |
| `text` | `plain`, `markdown`, `csv`, `json` | 扩展名 / `application/json` |
| `audio` | `wav`, `mp3`, `m4a`, … | MIME `audio/*` 或常见音频扩展名 |
| `video` | `mp4`, `webm`, `mov`, … | MIME `video/*` 或常见视频扩展名 |
| `archive` | `zip`, `tar`, `tar.gz`, … | 扩展名 / 常见压缩 MIME（展开策略见 [archive-expand.md](./archive-expand.md)） |
| `unknown` | `*` | 兜底（无法识别的类型） |

分类结果写入 `parse_results`（`profile=metadata`）的 `summary`：`file_kind`、`file_subtype`、`processing_routes`。

### 上传 vs 识别（能力边界）

| 层 | 策略 |
|----|------|
| **上传** | **无** MIME/扩展名白名单；图片、PPT/PPTX、音频等均可入库 |
| **分类 / 元数据** | 上表一等 kind 有专用提取器；`unknown` / 未注册路径走 `GenericMetadataExtractor` |
| **layout（文档/表格）** | 默认 MarkItDown **0.2.0**：Markdown + **多 block**（Excel 按 sheet；文档按标题/`\f` 分页）；读路径见 content_view 裁剪 |
| **tables（电子表格）** | `SpreadsheetTablesParser`：确定性 openpyxl/xlrd 提取 → `metadata.tables_json`（`spreadsheet_tables@v1`）；**不**反向解析 Markdown |
| **layout（图片）** | **不走** MarkItDown：`VisionDeepParser`（LiteLLM 视觉，与 deep 同源） |
| **layout（音/视频）** | `AudioLayoutParser` / `VideoLayoutParser`（LiteLLM 多模态） |

产品矩阵总表见 [product-principles.md § 上传与识别能力矩阵](./product-principles.md)。

## 工厂入口

```python
from app.parsers.factory import create_processing_factory, get_processing_factory

factory = get_processing_factory()  # 单例，首次调用时 bootstrap
# 或
factory = create_processing_factory()  # 显式创建（测试用）
```

### 核心 API

| 方法 | 说明 |
|------|------|
| `classify(...)` | 文件 → `FileClassification` |
| `resolve_metadata_extractor(classification)` | 层级 1 元数据处理器 |
| `resolve_parser(classification, profile)` | 指定 profile 的解析器 |
| `default_levels(classification)` | 该分类默认应跑哪些层级 |
| `build_routes(classification)` | 完整 `ProcessingRoute` 列表 |

解析器查找顺序：`(kind, subtype, profile)` → `(kind, *, profile)` → `(*, *, profile)`。

## 默认路由（当前实现）

| 分类 | 元数据处理器 | 默认异步层级 | 正文识别后端（写入 profile=layout） |
|------|-------------|-------------|-------------------------------|
| `pdf/digital` | `PdfDigitalMetadataExtractor` | metadata → layout | `LAYOUT_PARSER_NAME`（默认 markitdown） |
| `pdf/scanned` | `PdfScannedMetadataExtractor` | metadata → **ocr** → layout | 同上（ocr 与 layout 同引擎实例，除非图片类） |
| `image/document_scan` | `ImageMetadataExtractor` | metadata → **ocr** → layout | **VisionDeep**（LiteLLM） |
| `image/screenshot` / `image/photo` | `ImageMetadataExtractor` | metadata → layout；**deep** 按需 | **VisionDeep**（layout 与 deep 同源） |
| `spreadsheet/*` | `SpreadsheetMetadataExtractor`（xlsx+xls） | metadata → layout；**另可并行 tables**（`auto_parse.spreadsheet.also_tables`，默认 true） | layout：markitdown / simple / mineru；**tables：`SpreadsheetTablesParser`** |
| `office/*` | `OfficeMetadataExtractor`（**docx 深提取**；doc/ppt/pptx L0 字段浅） | metadata → layout | markitdown；旧式 doc/ppt/xls 先 LibreOffice |
| `text/*` | `TextMetadataExtractor` | metadata → layout | markitdown / simple |
| `audio/*` | `AudioMetadataExtractor` | metadata → layout | **AudioLayoutParser**（LiteLLM） |
| `video/*` | `VideoMetadataExtractor` | metadata → layout | **VideoLayoutParser**（LiteLLM） |
| `unknown/*` | `GenericMetadataExtractor` | metadata → layout（尽力） | markitdown 等 |

字段契约与扩展说明见 [metadata-extraction.md](./metadata-extraction.md)。

### 图片 / 演示文稿 / 音视频（实现现状）

| 类型 | 分类 | L0 元数据 | layout | 备注 |
|------|------|-----------|--------|------|
| 图片 png/jpeg/webp/gif/bmp 等 | `image/*` | Pillow 尺寸等 | ✅ **VisionDeep**（默认 auto_parse layout） | Agent 主路径读 `profile=layout`；`deep` 同源；自定义指令用 custom-parse |
| **`.pptx`** | `office/pptx` | 浅：`office_format=pptx`（无幻灯片数深字段） | ✅ MarkItDown | kind 已是 office，非 unknown |
| 旧 **`.ppt`** | `office/ppt` | 浅 | ✅ LibreOffice→pptx 后 layout | 见 [legacy-office.md](./legacy-office.md) |
| 音频 | `audio/*` | **AudioMetadataExtractor**（duration 等） | ✅ LiteLLM 转写 Markdown | 需配置 `audio_model` |
| 视频 | `video/*` | **VideoMetadataExtractor**（duration 等） | ✅ LiteLLM 理解 Markdown | 需配置 `video_model`；不依赖 ffmpeg |

文档类**正文识别后端**由 `LAYOUT_PARSER_NAME`（历史配置名）决定，结果写入 **`profile=layout`**（API 槽位名 = 基础正文，不是「版面引擎」分类）。**不**覆盖图片/音视频专用解析器：

| 值 | 角色 | 用途 | 产物形态 |
|----|------|------|----------|
| **`markitdown`（默认）** | 正文识别 | PDF / Office / 表格 / 文本等确定性转 Markdown | **Markdown 字符串**（Excel 为 GFM 表；pptx 为幻灯片文本） |
| `simple` | 正文识别 | 单元测试、无外部依赖 | 纯文本按行 → heading/paragraph blocks |
| `mineru` | 正文识别 | 外部 MinerU 高精度文档解析 | blocks + tables（MinerU content_list） |
| **`vision-pages`** | **正文识别**（页图多模态） | PDF / Office / 表格 → 页图 → LiteLLM（默认 `kimi-k2.6:cloud`） | **Markdown 字符串**（`metadata.markdown`；按页 `<!-- page N -->`）；**文本 kind 仍走 markitdown** |

> 命名约定：`profile=layout` / `layout_parser_name` 是历史 API/配置名；产品语义是 **L1 基础正文**。`vision-pages` 是正文识别后端之一，**不要**称作 layout 引擎。

### MarkItDown 产物约定（智能体）

**解析缓存（对象存储中的 layout 结果）** 保留引擎原始 Markdown，例如 Excel 可能含空白 Sheet2/Sheet3：

```json
{
  "source": { "parser_name": "markitdown", "profile": "layout" },
  "blocks": [
    { "id": "md-001", "type": "paragraph", "text": "## Sheet1\n| ... |\n\n## Sheet2\n|\n|  |", "metadata": { "format": "markdown" } }
  ],
  "tables": [],
  "metadata": {
    "format": "markdown",
    "markdown": "## Sheet1\n| ... |\n\n## Sheet2\n|\n|  |",
    "char_count": 2687,
    "filename": "outbound.xls",
    "mime_type": "application/vnd.ms-excel"
  }
}
```

**读路径（`GET /content`）** 默认 `smart_mode=true`，对表格类文件做智能体友好后处理后再返回：

```json
{
  "metadata": {
    "format": "markdown",
    "markdown": "## Sheet1\n| 编号 | 名称 |\n| --- | --- |\n| 1 | 系统A |\n",
    "char_count": 48,
    "filename": "outbound.xls",
    "mime_type": "application/vnd.ms-excel",
    "smart_mode": true,
    "smart_dropped_sheets": 2
  }
}
```

| 概念 | 说明 |
|------|------|
| 缓存键 | 仍为 `(content_hash, parser, version, profile)`；**不含** `smart_mode` |
| 过滤时机 | 仅 `GET /content`（及共用的 `filter_structured_content`），不回写缓存 |
| 适用范围 | spreadsheet MIME / `.xls` `.xlsx` `.csv` 等；PDF/Office 正文不误伤 |
| 关闭方式 | `smart_mode=false` 返回缓存中的原始全文 |

智能体最简用法：`markdown = content["metadata"]["markdown"]`（默认已去空 sheet）。  
当前**从简**：不产出 `[{field: value}, ...]` 关系记录；关系表以 Markdown 表形式交给 Agent 推理。

实现模块：`app/parsers/markitdown_layout.py`（转换）· `app/parsers/smart_markdown.py`（智能后处理）。

## 缓存键

解析结果缓存键为 `(content_hash, parser.name, parser.version, profile)`。  
**不同子类型可能绑定不同 parser**，因此缓存自然隔离，避免 scanned PDF 与 digital PDF 共用错误结果。  
`smart_mode` **不是**缓存维度：同一 layout 结果可在读路径按开关切换「智能 / 原始」。

## 扩展示例

为「表单 PDF」增加专用元数据与 layout 解析：

```python
# app/parsers/metadata_handlers.py
class FormPdfMetadataExtractor(PdfDigitalMetadataExtractor):
    name = "pdf-form-metadata"
    version = "0.1.0"
    # ...

# app/parsers/factory.py → create_processing_factory()
factory.register_metadata("pdf", "form", FormPdfMetadataExtractor())
factory.register_parser("pdf", "form", "layout", FormLayoutParser())
factory.register_default_levels("pdf", "form", [ProcessingLevel.METADATA, ProcessingLevel.LAYOUT])
```

分类逻辑扩展在 `app/parsers/classifiers.py` 增加 `form` 子类型判定。

## 相关文件

| 路径 | 职责 |
|------|------|
| `app/parsers/factory.py` | 工厂定义与 bootstrap |
| `app/parsers/classifiers.py` | 文件分类 |
| `app/parsers/types.py` | `FileKind`, `ProcessingLevel`, `FileClassification` |
| `app/parsers/metadata_handlers.py` | 各类型元数据提取器 |
| `app/parsers/markitdown_layout.py` | 文档/表格默认 layout：文件 → Markdown |
| `app/parsers/vision_deep.py` | 图片 layout / ocr / deep（LiteLLM 视觉） |
| `app/parsers/audio_layout.py` / `video_layout.py` | 音视频 layout |
| `app/parsers/structure_blocks.py` | layout 多 block 拆分（sheet / 标题 / 页） |
| `app/parsers/content_view.py` | content 读路径裁剪（outline / max_chars / sheets） |
| `app/parsers/smart_markdown.py` | **智能模式**：content 读路径去空 sheet |
| `app/parsers/simple_layout.py` | 测试用轻量 layout |
| `app/parsers/mineru_layout.py` | MinerU layout |
| `app/parsers/classification_store.py` | 从 DB 加载已存分类 |
| `app/parsers/registry.py` | 对外门面，委托工厂 |
| `app/services/parse_service.py` | 按分类解析 parser 并入队 |