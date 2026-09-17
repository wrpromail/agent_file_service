# 产品原则与处理契约

> 汇总 2026-07-14 产品讨论结论，作为定位、解析策略与智能体集成的权威约定。  
> 实现对照：[design.md](../design.md) · [metadata-extraction.md](./metadata-extraction.md) · [agent-integration.md](./agent-integration.md) · [processing-factory.md](./processing-factory.md)

---

## 1. 服务是什么、不是什么

### 一句话定位

> **Agent File Service = 带内容寻址的文件中台 + 可缓存的文档事实层。**  
> 不是网盘，也不是文档业务 Agent。

### 两层同心圆

```text
┌─────────────────────────────────────────────┐
│  外圈：不做                                   │
│  业务解读 · RAG/向量 · 知识图谱 · 合规审核     │
│  业务语义切分 · 主动回调订阅方                 │
├─────────────────────────────────────────────┤
│  中圈：文档事实层                             │
│  分类 · 元数据 · OCR · layout · 结构块 · MD  │
│  （可缓存、可版本化、可 profile 升级）          │
├─────────────────────────────────────────────┤
│  内圈：文件管理底座                            │
│  上传 · 去重 · 引用 · 生命周期 · 下载 · clone  │
└─────────────────────────────────────────────┘
```

| 层 | 职责 | 若缺失的后果 |
|----|------|--------------|
| **文件管理底座** | Blob 去重、Reference 隔离、GC、clone | 无法成为跨 Agent 共享基础设施 |
| **文档事实层** | 元数据、layout/OCR、统一 schema | 各 Agent 重复解析、格式地狱 |

两者**缺一不可**：只做管理则价值偏窄；只做智能而无去重/引用则无法横切复用。

### 原则回顾（与 design.md 一致）

1. **只供给事实，不做业务解读**
2. **统一格式，而非统一答案**（保留 confidence / warnings）
3. **物理去重，引用隔离**
4. **渐进式处理，成本可控**
5. **无状态，不感知订阅方**
6. **交付句柄，而非搬运内容**

---

## 2. 核心产品决策：保证线 × 能力菜单

讨论焦点：**上传后后台狂解析** vs **智能体全靠工具触发** —— 两者皆不可取。

| 极端 | 问题 |
|------|------|
| 上传后持续跑「各种解析」 | 成本不可控；服务在猜意图；能力越多越贵 |
| 一切全靠 Agent 记得 trigger | 默认读不到字；每个 Agent 重复拼流水线 |

### 选定模型（混合）

```text
保证线（服务主动）          能力菜单（服务被动 / 工具按需）
─────────────────          ─────────────────────────────
L0 metadata  同步必须       deep / 视觉理解
L1 基础正文  默认后台       换正文识别后端（markitdown / mineru /
  (API profile=layout)      vision-pages 等)；force 重解析
  （auto_parse）            扫描件可走 ocr；未来更贵的 profile
```

**命名：** API/`parse_results` 里 L1 槽位仍叫 `profile=layout`（历史兼容）；产品语义是**基础正文**。`layout_parser_name` 选的是**正文识别后端**，其中 `vision-pages` 是页图多模态正文识别，**不是**版面 layout 引擎。

**一句话契约：**

> **上传即可得句柄 + 元数据；默认尽快备好基础正文（profile=layout）；更重的解析是可发现的工具能力，由智能体按任务触发；业务结论永远在 Agent。**

### 对 Agent 的明确承诺

| | 承诺 |
|--|------|
| **保证** | `metadata` 上传响应中即有；`layout` 在默认 `auto_parse=true` 时后台准备，可用 `status` 查询 |
| **不保证** | `deep` / 特殊引擎 / 无人请求的昂贵 profile 已完成 |
| **Agent 负责** | 需要时 `trigger_file_parse(profile)`；业务推理、字段抽取、是否入库 |

### 默认策略表

| 文件类型 | 保证线（自动） | 能力菜单（按需） |
|----------|----------------|------------------|
| 数字 PDF / docx / pptx / txt / md | metadata + layout | deep（通常不需要） |
| **xls / xlsx / csv** | metadata + layout；**默认可并行 tables**（`also_tables`） | `POST /parse?profile=tables` 或关 `also_tables` 后按需 |
| 扫描 PDF / 扫描图 | metadata；（工厂默认可含 **ocr**）+ layout | `POST /parse?profile=ocr`、mineru 引擎、deep |
| 常见图片（png/jpeg/webp/gif/bmp 等） | metadata（尺寸等）+ 默认 **layout=视觉模型 → Markdown**（LiteLLM，还原版面结构） | deep（同源视觉）；custom-parse |
| 音频 / 视频 | metadata（**duration 等 L0**）+ layout（LiteLLM 多模态 → **Markdown**，需配置） | custom-parse 等 |
| **旧式 Office（doc/ppt/xls）** | metadata + **layout**（LibreOffice→OOXML 后 MarkItDown） | 见 [legacy-office.md](./legacy-office.md) |
| 未知类型 | metadata + 尽力 layout | 失败则 download 由 Agent 处理 |
| **zip / tar / tar.gz** | 存父包；过滤隐藏与不支持类型后，可识别成员 **≤8** 则展开为子 `reference_id` 并按类型识别；过多则只存父包（`archive.status=too_many`） | 可选 `archive_password`（ZipCrypto）；嵌套压缩包不递归 |

`auto_parse=false`：仅「只存不解析」场景（归档、稍后再说）；**不是**推荐主路径。

**压缩包与 Agent**：父包句柄保留可 download；**发给智能体的应优先用 `archive.children[]` 子句柄**（与多文件上传相同）。契约全文：[archive-expand.md](./archive-expand.md)；样例：[../sample/README.md](../sample/README.md)。

### 上传与识别能力矩阵（当前）

> **上传层**与**事实层**要分开看：上传默认**不设 MIME 白名单**（常见图片 / PPT / 音频都能入库）；识别深度取决于分类、元数据提取器与 MarkItDown / deep 管线。

| 类型 | 上传（blob + 引用） | 分类 kind | 同步元数据 L0 | layout → Markdown | 能力菜单 |
|------|---------------------|-----------|---------------|-------------------|----------|
| 常见图片 | ✅ | `image`（photo / screenshot / document_scan） | ✅ 宽高、格式等 | ✅ layout=视觉模型（Agent 读 layout） | deep 同源；custom-parse |
| **`.pptx` / `.docx` / 表格** | ✅ | office / spreadsheet | ✅ docx/表格深；**pptx L0 仅 `office_format` 等浅字段** | ✅ MarkItDown；**xlsx/xls 另有 `profile=tables` JSON** | tables 默认可 auto（`also_tables`） |
| 旧版 **`.ppt` / `.doc` / `.xls`** | ✅ | office/ppt、office/doc、spreadsheet/xls | ⚠️ doc/ppt 有限；xls 有 sheet | ✅ LibreOffice→OOXML（worker） | 原件存储；OOXML 为派生产物 |
| 金山 WPS **`.wps` / `.et` / `.dps`**（及 `.wpt`/`.ett`/`.dpt`） | ✅ | office/wps、spreadsheet/et、office/dps | ⚠️ 浅字段（format 标记） | ⚠️ LibreOffice→OOXML（**尽力而为**） | 原件存储；复杂版式可能失败 |
| 音频 / 视频 | ✅ | audio / video | ✅ duration 等（wav/stdlib；其它依赖 mutagen） | ⚠️ LiteLLM layout（需模型） | custom-parse |
| PDF / 文本 | ✅ | 一等公民 | ✅ | ✅ | 见上表 |

**要点：**

1. **图片**一等公民；默认 auto_parse **profile=layout**，引擎为 LiteLLM 视觉模型（与 `deep` 同源，便于调用方只拉 layout）。自定义指令用 **custom-parse**。  
2. **旧式 Office / WPS** 在 worker 内经 LibreOffice 转 OOXML；下载仍是原件。WPS 为尽力而为，见 [legacy-office.md](./legacy-office.md)。  
3. **音视频 layout** 依赖 LiteLLM 配置；未配置时可能失败，但仍可上传/download。  
4. 路由细节见 [processing-factory.md](./processing-factory.md)；Agent 约定见 [agent-integration.md](./agent-integration.md)。

### 角色分工

| 角色 | 需要知道什么 |
|------|----------------|
| **File Service** | 有哪些 profile、默认跑哪些、缓存键、`processing_routes` |
| **Agent / Runtime** | MCP/REST 工具 schema；何时 status / content / trigger |
| **编排层** | 上传策略、`session` 挂接、clone 共享、权限 |

智能体**不必**从零拼解析流水线；只需：

1. 默认消费保证线产物（metadata + layout Markdown）
2. 按任务从能力菜单加料（ocr / deep / …）

---

## 3. 元数据与「文档智能划分」边界

### 元数据（L0）— 事实层第 0 步

- **同步、廉价、可缓存**，失败降级且**不阻断上传**
- 按类型分流：PDF / Spreadsheet / Office / Text / Image（见 [metadata-extraction.md](./metadata-extraction.md)）
- 输出：页数、sheet、尺寸、编码、文本层、文档属性、`recommended_profiles` 等

### 结构切分 vs 业务切分

| 类型 | 是否属于本服务 | 示例 |
|------|----------------|------|
| **结构切分** | ✅ 属于事实层（layout 及后续增强） | 按页、标题树、段落块、表格区域、稳定 block id |
| **业务切分** | ❌ Out of Scope | 「招标商务卷 vs 技术卷」、甲方/金额字段、合规条目 |

后续「智能划分」应增强 **layout 的结构块模型**，而不是把业务语义沉进 metadata 或上传流水线。

**Phase1 已落地的结构/读路径能力**：Excel 按 sheet 多 block；Markdown 按标题/分页符拆分；`GET /content` 支持 `mode=outline`、`max_chars`/`offset`、`sheets` 过滤（不改写缓存）。

---

## 4. 智能体推荐交互心智

```text
用户上传文件
    ↓
File Service：L0 metadata 同步 + 返回 reference_id
    ↓（默认 auto_parse=true）
File Service：L1 layout（及类型默认的 ocr 等）入队
    ↓
Agent 收到 [file:reference_id filename]
    ↓
get_file_status → layout ready?
    ├─ 是 → get_file_content(layout) → 读 Markdown → 业务任务
    └─ 否 → 短暂等待 / 提示「解析中」后再拉
    ↓
任务是否需要更多事实？
    ├─ 扫描件读不清 → trigger(ocr) 或依赖默认 ocr 结果
    ├─ 需要看图理解 → trigger(deep)
    └─ 业务字段 / 审核结论 → Agent 自己完成（不回调 file service 做业务）
```

### 能力菜单（工具视角）

| 能力 | REST / MCP | 说明 |
|------|------------|------|
| 查状态与元数据 | `GET .../status` · `get_file_status` | 保证线进度 |
| 取基础正文 | `GET .../content?profile=layout` · `get_file_content` | **主路径**；默认 `smart_mode=true`（Excel 去空 sheet） |
| 取表格 JSON | `GET .../content?profile=tables` | spreadsheet；`metadata.tables_json`（headers+rows）；默认可 auto |
| 触发解析 | `POST .../parse` · `trigger_file_parse` | 加料 / force（`layout`/`ocr`/`deep`/`tables`） |
| 取深度结果 | `content?profile=deep` | 需先完成 deep |
| 原始文件 | `GET .../download` · `download_file` | 少数场景 |
| 列表 / clone | `GET /files` · `POST .../clone` | 编排层共享 |

**智能模式（读路径）**：保证线交付的 Markdown 应对齐「事实完整、噪音最小」。Excel 默认空白 sheet 等无信息量段落在 `smart_mode=true`（默认）下不进入 Agent 上下文；需要引擎全量输出时显式 `smart_mode=false`。过滤发生在 content 读路径，不改写解析缓存，也不属于业务解读。

详细集成步骤见 [agent-integration.md](./agent-integration.md)。

---

## 5. 明确不做（Out of Scope 清单）

- 业务语义关系建模、向量化、RAG、知识图谱  
- 合规审核、报销校验等带业务意图的深度分析  
- 业务语义切分与业务字段抽取  
- 主动推送/回调「解析完成」（由编排层轮询或自建事件）  
- 跨智能体权限判定（由 Runtime / A2A 决策后调用 clone）  
- 把 Agent 工作区 materialize 进本服务（会话进盘由调用方完成）

---

## 6. 相关文档索引

| 文档 | 内容 |
|------|------|
| [design.md](../design.md) | 总设计：原则、数据模型、三阶段流程 |
| [metadata-extraction.md](./metadata-extraction.md) | 分类型元数据字段契约 |
| [processing-factory.md](./processing-factory.md) | kind/subtype/level 路由 |
| [agent-integration.md](./agent-integration.md) | REST/MCP 与 Agent 使用约定 |
| [architecture-notes.md](./architecture-notes.md) | 实现取舍与流式/GC 等 |
| [implementation-status.md](./implementation-status.md) | 完成度看板 |
