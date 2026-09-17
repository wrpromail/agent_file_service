# 元数据提取（Metadata Extraction）

> 对应实现：`app/parsers/metadata_handlers.py` · 路由：`ProcessingFactory`  
> 上传路径：`MetadataParser` → 按 kind/subtype 选择提取器 → 结果写入 `parse_results(profile=metadata)`  
> 产品总契约（保证线 / 能力菜单）：**[product-principles.md](./product-principles.md)**

## 1. 在产品中的位置

元数据属于 **保证线 L0**（上传同步必须完成），是文档事实层的第 0 步。

Agent File Service 不是纯网盘，也不是业务文档 Agent：

```text
文件管理底座（Blob 去重 · Reference 隔离 · 生命周期）
        +
文档事实层（元数据 L0 · layout L1 · OCR/deep 能力菜单）
        →
智能体自行做业务解读 / RAG / 审核
```

| 在范围内（事实） | 不在范围内（业务） |
|------------------|-------------------|
| MIME、页数、sheet 数、尺寸、编码 | 「这是合同还是发票」 |
| 是否有文本层 / 是否建议 OCR | 合规结论、风险评分 |
| 标题/作者等**文档属性**（若文件自带） | 向量化、知识图谱 |
| 结构切分线索（标题预览、sheet 名；细切在 layout） | 按业务意图的字段抽取 |
| 推荐后续 profile（`recommended_profiles`） | 主动回调订阅方 |

**原则：只供给事实，不做业务解读。** 元数据上传同步、毫秒级、可缓存；**不等于**上传后跑完所有解析。

## 2. 处理位置

```text
上传（保证线启动）
  → classify_file()                 # kind + subtype
  → resolve_metadata_extractor()    # L0：同步元数据（本层）
  → store blob + reference
  → auto_parse? → schedule layout/ocr   # L1：默认后台，非「全部 profile」
  → deep 等 → 仅 Agent 工具触发          # 能力菜单
```

元数据提取器失败时**不得阻断上传**：降级为 MIME + size，并在 `extra.extractor_warning` 中注明原因。

## 3. 按类型的字段契约

所有提取器都会附带分类字段（由 `MetadataParser` / classification 注入）：

| 字段 | 说明 |
|------|------|
| `file_kind` | `pdf` / `spreadsheet` / `office` / `text` / `image` / `audio` / `video` / `archive` / `unknown` |
| `file_subtype` | 如 `digital`、`xlsx`、`docx`、`pptx`、`plain`、`mp3`、`mp4` |
| `recommended_profiles` | 建议的后续解析 profile 列表 |
| `processing_routes` | 工厂给出的完整路由（含 handler 名） |
| `extractor_warning` | 可选；解析库缺失或读失败时的降级说明 |

### 3.1 PDF（`pdf-digital-metadata` / `pdf-scanned-metadata`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `page_count` | int | 页数（API 顶层 `FileMetadata.page_count`） |
| `encrypted` | bool | 是否加密 |
| `text_layer` | `present` / `absent` | digital vs scanned |
| `first_page_text_chars` | int | 首页可提取文本字符数 |
| `ocr_recommended` | bool | scanned 为 true |
| `document.title/author/...` | object | PDF 内嵌文档信息（有则填） |

### 3.2 Spreadsheet（`spreadsheet-metadata`）

| 字段 | 说明 |
|------|------|
| `sheet_count` | sheet 数量（API 顶层） |
| `sheet_names` | 名称列表 |
| `sheets[]` | `{name, max_row, max_column, dimensions?}` |
| `workbook_format` | `xlsx` 或 `xls` |

- `.xlsx`：`openpyxl`（read_only）
- `.xls`：`xlrd`

### 3.3 Office（`office-metadata`）

| 字段 | 说明 |
|------|------|
| `office_format` | `docx` / `doc` / `pptx` / `ppt` 等 |
| `paragraph_count` / `non_empty_paragraph_count` | 段落统计（**docx 深提取**） |
| `table_count` | 表格数（docx） |
| `heading_count` / `headings_preview` | 标题样式预览（最多 10；docx） |
| `document.title/author/...` | 核心属性（docx） |

| 子类型 | L0 深度 | layout |
|--------|---------|--------|
| `.docx` | 深：段落/表格/标题预览/文档属性 | MarkItDown |
| 旧 `.doc` | 浅：`legacy_doc_limited_metadata` | LibreOffice→docx 后 layout |
| **`.pptx`** | 浅：`office_format=pptx`（**无**幻灯片数等专用深字段） | MarkItDown → 幻灯片 Markdown |
| 旧 **`.ppt`** | 浅：`office_format=ppt` | LibreOffice→pptx 后 layout |

分类器将 **Word + 演示文稿** 均标为 `office/*`（非 `unknown`）。pptx/ppt 走本提取器，但深字段目前仅 docx 填充。

### 3.4 Text（`text-metadata`）

| 字段 | 说明 |
|------|------|
| `line_count` / `char_count` | 行数与字符数 |
| `encoding` | 探测到的编码（utf-8 / gb18030 等） |
| `is_empty` | 是否空白 |
| CSV：`csv_column_count` / `csv_row_count` / `csv_dialect` | 轻量 CSV 提示 |
| Markdown：`markdown_heading_count` / `headings_preview` | AT 标题预览 |

### 3.5 Image（`image-metadata`）

| 字段 | 说明 |
|------|------|
| `width` / `height` | 像素尺寸 |
| `image_format` / `image_mode` | Pillow 格式与模式 |
| `frame_count` | 动图帧数（若有） |
| `ocr_recommended` | `document_scan` 为 true |

常见 raster（png/jpeg/webp/gif/bmp 等）在 `image/*` 下一等公民。  
**L0** 只给尺寸/格式等廉价事实；**视觉 OCR/简述**在保证线 **layout**（`VisionDeepParser`，需 LiteLLM），不在本元数据层。`deep` 与 layout 同源，按需 trigger。

### 3.6 Audio（`metadata_audio`）

| 字段 | 说明 |
|------|------|
| `duration_seconds` | 时长（wav 经 stdlib `wave`；其它容器可选 mutagen） |
| `sample_rate` / `channels` | 采样率 / 声道 |
| `audio_format` | 容器/解析器名 |
| `extractor_warning` | 失败降级标记（不阻断上传） |

**layout（保证线 L1）**：`AudioLayoutParser` 经 LiteLLM 转写为 Markdown（需配置 `audio_model`）。自定义指令用 **custom-parse**。

### 3.7 Video（`metadata_video`）

| 字段 | 说明 |
|------|------|
| `duration_seconds` | 有 mutagen 且可解析时 |
| `video_format` | 解析器类型名 |
| `extractor_warning` | 失败降级；**不**依赖 ffmpeg |

**layout**：`VideoLayoutParser` 经 LiteLLM 理解/转写为 Markdown（需配置 `video_model`）。自定义指令用 **custom-parse**。

### 3.8 Generic / Unknown（`metadata`）

仅 `mime_type`、`size_bytes`、`filename`、`extension` + 分类字段。

**会落到本路径的典型类型**（pptx / 音视频 **不再**走 generic）：

| 类型 | 上传 | L0 | layout 期望 |
|------|------|-----|-------------|
| 无法分类的二进制 / 冷门格式 | ✅ | generic | 尽力 MarkItDown 等，失败不阻断上传 |
| 压缩包父文件（展开见 archive） | ✅ | 分类为 archive 时见 archive 路径；否则 generic | 父包通常不跑 layout；子文件各自识别 |

## 4. 工厂注册一览

| 分类 | 提取器 | 默认后续层级 |
|------|--------|--------------|
| `pdf/digital` | PdfDigitalMetadataExtractor | metadata → layout |
| `pdf/scanned` | PdfScannedMetadataExtractor | metadata → ocr → layout |
| `spreadsheet/*` | SpreadsheetMetadataExtractor | metadata → layout |
| `office/*` | OfficeMetadataExtractor | metadata → layout |
| `text/*` | TextMetadataExtractor | metadata → layout |
| `image/document_scan` | ImageMetadataExtractor | metadata → ocr → layout（视觉引擎） |
| `image/*` | ImageMetadataExtractor | metadata → layout（视觉）；deep 按需 |
| `audio/*` | AudioMetadataExtractor | metadata → layout（LiteLLM 转写） |
| `video/*` | VideoMetadataExtractor | metadata → layout（LiteLLM） |
| `*/*` | GenericMetadataExtractor | metadata → layout |

## 5. API 可见性

上传 / 详情响应中的 `metadata`：

```json
{
  "filename": "采购文件.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 383540,
  "page_count": 22,
  "sheet_count": null,
  "extra": {
    "file_kind": "pdf",
    "file_subtype": "digital",
    "text_layer": "present",
    "first_page_text_chars": 96,
    "recommended_profiles": ["layout"],
    "document": { "title": "..." },
    "processing_routes": [ ... ]
  }
}
```

完整 summary 同时落在 `parse_results` 中 `profile=metadata` 的 `summary` JSON。

## 6. 扩展方式

1. 在 `metadata_handlers.py` 新增提取器类（继承 `GenericMetadataExtractor` 或 `MetadataExtractor`）
2. 在 `create_processing_factory()` 中 `register_metadata(kind, subtype, extractor)`
3. 如需新 subtype，先改 `classifiers.py`
4. 补充 `tests/test_metadata_handlers.py` 契约测试

## 7. 与「文档智能划分」的边界

| 阶段 | 触发 | 职责 | 当前状态 |
|------|------|------|----------|
| **metadata（本层）** | 保证线 L0 同步 | 廉价事实：页数、sheet、尺寸、编码、文档属性 | ✅ 已实现并按类型分流 |
| **layout（结构切分）** | 保证线 L1 默认后台 | 标题树、段落块、表格区、Markdown | ✅ 多 block 最低可用（sheet/标题/页）；更细 bbox 后续 |
| **content 智能模式** | 读路径默认 `smart_mode=true` | 表格丢弃无内容 sheet 等噪音后处理 | ✅ 见 [agent-integration.md](./agent-integration.md) |
| **content 裁剪** | 读路径 | outline / max_chars / offset / sheets | ✅ `content_view.py` |
| **ocr / deep 等** | 能力菜单（或类型默认 ocr） | 扫描识别、视觉理解等 | ✅ API 正式支持 ocr + deep |
| **业务切分** | — | 招标「商务卷/技术卷」、字段抽取 | ❌ 明确不在本服务 |

说明：metadata 层的 `sheet_count` / `sheet_names` 仍是工作簿物理事实（含空 sheet）；智能模式只影响 **content 正文展示**，不改写元数据中的 sheet 列表。

后续若做「智能划分」，优先增强 **layout 的结构块模型**（稳定 block id、页码、标题层级），而不是把业务语义沉进 metadata，也不是在上传后无差别跑满所有 profile。完整产品讨论见 [product-principles.md](./product-principles.md)。
