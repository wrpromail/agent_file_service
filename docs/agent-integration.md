# 智能体集成指南

本文说明多智能体系统如何通过 **REST API** 或 **MCP 工具** 使用 file service。

产品定位与「保证线 / 能力菜单」完整讨论见 **[product-principles.md](./product-principles.md)**；元数据字段见 [metadata-extraction.md](./metadata-extraction.md)。

## 产品契约（Agent 必读）

File service **不是**「上传后后台无限解析一切」，也**不是**「你不 trigger 就什么都没有」。

| 层级 | 谁触发 | 内容 | Agent 预期 |
|------|--------|------|------------|
| **保证线 L0** | 上传同步 | 分类型元数据（含音视频 duration 等） | 响应里就有 `metadata` |
| **保证线 L1** | 默认 `auto_parse=true` 后台 | layout → Markdown（扫描件可默认 ocr） | `status` 查进度，就绪后 `content` |
| **能力菜单** | Agent 工具显式调用 | `ocr` / `deep`、force 重解析；`tables` 也可显式 trigger | 需要再 `trigger_file_parse` |
| **spreadsheet 扩展** | 默认 auto（`also_tables`） | `profile=tables` → headers+rows JSON | 读 `content?profile=tables` 的 `metadata.tables_json` |

**对 Agent 的承诺：**

- **保证**：metadata 上传即有；layout 默认会准备（可关 `auto_parse`）。
- **不保证**：deep / 高精度引擎在无人请求时已完成。
- **你负责**：业务解读、字段抽取、是否入库；需要时从能力菜单加料。

### 推荐心智循环

```text
[file:reference_id name]
  → get_file_status          # 元数据 + layout/ocr/deep 状态
  → layout ready?
       是 → get_file_content(layout) → 用 Markdown 做任务
       否 → 等待 / 提示解析中
  → 不够？ → trigger_file_parse(ocr|deep|…) → 再 content
  → 业务结论只在 Agent 内完成
```

## 核心模型

| 概念 | 说明 |
|------|------|
| `reference_id` | 租户/会话内稳定句柄（智能体应使用此 ID） |
| `content_hash` | 物理去重键（服务内部使用，智能体不应直接访问） |
| `session_id` | 会话维度，通常映射智能体 `thread_id` |
| `profile` | 解析层级：`metadata`（保证线）/ `layout`（保证线）/ **`tables`**（spreadsheet 结构化 JSON，默认可 auto）/ `ocr` / `deep`（能力菜单） |
| `auto_parse` | 上传参数，默认 `true`：入队保证线异步解析；`false` 仅存文件。**仍受**服务端分类型开关/门槛约束（见 [configuration.md](./configuration.md) 自动 layout） |
| `custom-parse` | 图片/音频/**视频**：`POST /v1/files/{id}/custom-parse` 传用户 `prompt`；落库 `custom_parse_jobs`，与共享 `parse_results` 隔离 |

智能体看到用户消息中的 `[file:reference_id filename]` 后，**优先**走保证线，再按需加料：

1. 查简介与进度 → `GET /status` 或 MCP `get_file_status`（含元数据、`recommended_profiles`）
2. **拉基础 Markdown** → `GET /content?profile=layout`（**主路径**）
3. **表格结构化数据**（xlsx/xls/csv）→ `GET /content?profile=tables` → `metadata.tables_json`（`spreadsheet_tables@v1`；默认可与 layout 并行 auto）
4. 内容不足时触发 → `POST /parse?profile=ocr|deep|tables` + 再拉 content
5. 少数场景下载原件 → `GET /download` 或 MCP `download_file`

### 推荐：直接消费 Markdown（默认 layout）

默认 `LAYOUT_PARSER_NAME=markitdown`。`profile=layout` 完成后：

```text
GET /v1/files/{reference_id}/content?profile=layout
# 等价于 smart_mode=true（默认）
```

```python
# 伪代码：智能体 / 前端工具侧
content = get_file_content(reference_id, profile="layout")  # smart_mode 默认 true
# 方案 B：未就绪也是 HTTP 200，用 ready 判断，不要把「解析中」当错误码
if not content.get("ready", True):
    # content["message"] 已是用户可读文案，如「内容识别进行中，请稍后再试。」
    # content["message_code"]: parse_not_started | parse_pending | parse_processing | parse_failed
    return content["message"]
markdown = content["metadata"]["markdown"]  # 完整 Markdown 字符串
# Excel：默认已去掉空白 Sheet2/Sheet3 等噪音；需要全量时 smart_mode=false
# 等价：content["blocks"][0]["text"]
# 业务字段抽取、审核结论：在 Agent 内完成，不要期望 file service 返回
```

#### content 就绪 envelope（方案 B）

| 字段 | 说明 |
|------|------|
| `ready` | `true` 正文可用；`false` 未开始/排队/进行中/失败 |
| `status` | 与 parse 状态对齐：`completed` / `not_started` / `pending` / `processing` / `failed` |
| `message` | 面向用户的中文提示（`ready=false` 时必有） |
| `message_code` | 机器可读：`parse_not_started` / `parse_pending` / `parse_processing` / `parse_failed` / `parse_not_ready` |

**约定**：解析未完成**不是** HTTP 错误。404/401/5xx 留给「不存在 / 鉴权 / 服务故障」。UI 点文件名预览应展示 `message`，而不是把 409 当失败。

| 文件类型示例 | Markdown 形态 | Agent 注意 |
|--------------|----------------|------------|
| `.xls` / `.xlsx` | `## Sheet名` + GFM 表格 | 默认 `smart_mode` 去空 sheet；**结构化行数据**用 `profile=tables`（`tables_json`） |
| `.docx` / `.pdf` / `.txt` | 标题与段落 Markdown | 保证线主路径 |
| **`.pptx` / 旧 `.ppt`** | 幻灯片 Markdown | `.ppt` 经 LibreOffice→pptx（worker） |
| 常见 **图片** | layout=视觉模型（OCR+简述，需 LiteLLM） | 主路径 `content?profile=layout`；自定义指令用 **custom-parse**；`deep` 同源 |
| **音频 / 视频** | LiteLLM layout（需配置） | 未配置模型时可能失败；仍可 download；自定义指令用 custom-parse |

**上传能力**：API **不按类型拒绝**常见图片、PPTX、音频；「能否读出字」见上表。完整矩阵：[product-principles.md § 上传与识别能力矩阵](./product-principles.md)。

**从简约定**：file service 只负责「文件 → 事实（元数据 + Markdown/结构块）」；关系型 `[{...}]` 字段抽取、业务校验、业务语义切分由智能体框架侧完成。

### 智能模式（`smart_mode`，默认开启）

智能体主路径应对齐 **「事实完整、噪音最小」**。`GET /content` 增加查询参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `smart_mode` | `true` | 读路径后处理；**不改写**解析缓存中的原始 Markdown |

**当前能力（表格 / Excel）**

Excel 新建工作簿常带 3 个默认 sheet；MarkItDown 会把空白 sheet 也转成噪音段，例如：

```text
## Sheet1
| 编号 | 名称 |
| --- | --- |
| 1 | 系统A |

## Sheet2
|
|  |

## Sheet3
|
|  |
```

`smart_mode=true` 时仅保留有实质单元格内容的 sheet（上例只保留 Sheet1）。判定规则：sheet 正文去掉空行、对齐分隔行、仅含 `|` / 空白的表行后，若无任何非空单元格文本，则整段丢弃。

| 请求 | 结果 |
|------|------|
| `.../content?profile=layout` 或 `smart_mode=true` | 过滤后的 Markdown（推荐 Agent） |
| `.../content?profile=layout&smart_mode=false` | 引擎原始全量 Markdown |

响应 `metadata` 增量字段（有过滤时）：

| 字段 | 说明 |
|------|------|
| `smart_mode` | 是否应用了智能后处理 |
| `smart_dropped_sheets` | 丢弃的 `## Sheet*` 段数量（若有） |
| `markdown` / `char_count` | 与过滤后正文一致 |

实现：`app/parsers/smart_markdown.py`（`filter_structured_content` → `apply_smart_mode_to_content`）。  
解析侧仍写入完整原始产物，保证 `smart_mode=false` 可还原。

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活 + 依赖状态（`?deep=true` 重探测）；免鉴权 |
| GET | `/metrics` | Prometheus text；免鉴权（**仅内网 scrape**） |
| POST | `/v1/files` | 上传；zip/tar 见下方「压缩包」；Form 可选 `archive_password` |
| POST | `/v1/files/from-url` | 从 URL 拉取入库（SSRF 防护；见 configuration） |
| POST | `/v1/files/batch-status` | 批量 status（单次最多 `batch_max_ids`，默认 20；逐项错误） |
| POST | `/v1/files/batch-content` | 批量 content（支持裁剪参数） |
| GET | `/v1/files` | 按元数据过滤查询（`session_id`、`agent_id`、`uploaded_by`、`filename`、`content_hash`、分页） |
| GET | `/v1/files/{id}` | 详情 |
| PATCH | `/v1/files/{id}` | 更新 `session_id` / `agent_id` / `custom_metadata` / `expires_at`（默认 merge） |
| DELETE | `/v1/files/{id}` | 软删引用（触发 ref_count / GC 路径） |
| GET | `/v1/files/{id}/status` | 元数据 + 解析状态；**ETag** / `If-None-Match` → 304 |
| GET | `/v1/files/{id}/content` | 结构化内容；未就绪 **200 + ready=false**；`smart_mode`；**裁剪见下**；ETag |
| GET | `/v1/files/{id}/download` | **原始文件二进制** |
| POST | `/v1/files/{id}/parse` | 触发 `layout` / **`ocr`** / `deep` / **`tables`**（可 `force`） |
| POST | `/v1/files/{id}/custom-parse` | 图/音/视频用户 `prompt`；独立 job |
| GET | `/v1/files/{id}/custom-parse` | 列出 custom-parse 任务 |
| GET | `/v1/files/{id}/custom-parse/{job_id}` | 查询单次任务（含 `result_text`） |
| GET | `/v1/files/{id}/archive/members` | 压缩包列目录（`too_many` 时探查成员） |
| POST | `/v1/files/{id}/clone` | **引用克隆**：为目标 tenant 生成新句柄，共享同一 blob 及解析结果 |
| GET | `/v1/sessions/{id}/files` | 会话文件列表 |

请求头（**鉴权不可关闭**）：

```
Authorization: Bearer <api_key>
X-Tenant-Id: <tenant>
```

| 配置 | 说明 |
|------|------|
| `API_KEYS` | 逗号分隔多个 key；与 `Authorization` 匹配。空则服务启动自动生成 ephemeral key 并打日志 |
| `ALLOWED_TENANT_IDS` | 可选。配置后仅列表内 `X-Tenant-Id`（及 clone 的 `target_tenant_id`）合法；未配置则调用方自报 |
| `API_KEY_TENANT_MAP` | 可选。`key1:tenant-a\|tenant-b,key2:*`；绑定 key 可用租户 |

### Content 裁剪（Agent 主路径）

`GET /v1/files/{id}/content` 查询参数（读路径，**不改写** parse 缓存）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `profile` | `layout` | `layout` / `ocr` / `deep` / **`tables`** |
| `smart_mode` | `true` | Excel **layout** Markdown 去空 sheet（对 `tables` profile 不适用） |
| `mode` | `full` | `full` 正文；`outline` 仅标题/sheet 大纲（主要对 layout） |
| `max_chars` | 不限 | 字符窗口上限；响应 `metadata.truncated` / `next_offset` |
| `offset` | `0` | 字符偏移（在 smart_mode / sheets 之后） |
| `sheets` | 无 | 逗号分隔 sheet 名；layout 与 **tables** 均可过滤 |
| `pages` / `types` | 无 | 按页 / block 类型过滤（多 block 时有效） |

#### 表格 profile（`tables`）

| 点 | 说明 |
|----|------|
| 何时有 | spreadsheet 上传且 `auto_parse.spreadsheet.also_tables=true`（默认）时与 layout **并行**入队；也可 `POST /parse` `{"profile":"tables"}` |
| 读法 | `GET .../content?profile=tables` → `metadata.tables_json`（schema `spreadsheet_tables@v1`：sheets[].headers / rows） |
| 过滤 | `?sheets=应用系统` 只保留匹配 sheet |
| 与 layout 关系 | **独立缓存键**；layout 是 Markdown 主路径；tables 供 codegen / 精确取数，非 Markdown 反解 |

轮询：带上次响应的 `ETag` 发 `If-None-Match`，未变则 **304**。

### 压缩包（zip / tar / tar.gz）

完整契约见 **[archive-expand.md](./archive-expand.md)**。

| 点 | Agent / 编排约定 |
|----|------------------|
| 上传 | `POST /v1/files`；加密 zip 加 Form `archive_password`（ZipCrypto；不落库） |
| 响应 | `archive.status` + `archive.children[]`（每项有独立 `reference_id`） |
| **发给模型** | `status=expanded` 时 **只挂 children** 为多个 `[file:id name]`，与多选上传相同 |
| 过多 / 空 | `too_many` / `empty`：仅父包可 download；可用 `GET .../archive/members` 列目录 |
| 识别 | 每个子文件默认 auto_parse（layout）；父包不跑 layout |
| 样例 | `sample/sample.zip`、`sample.tar.gz`、`sample-password.zip`（密码 `secret123`） |

```bash
# 加密 zip → 看 children
curl -s -X POST http://localhost:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/sample-password.zip" \
  -F "archive_password=secret123" | jq '.archive'
```

### 下载原始文件

```bash
curl -H "Authorization: Bearer dev-api-key" \
     -H "X-Tenant-Id: dev-user" \
     -o notes.txt \
     http://localhost:8020/v1/files/{reference_id}/download
```

返回 `Content-Disposition: attachment`，body 为对象存储中的原始 blob。

## MCP 封装（可选）

本仓只提供 REST。调用方可把同一组能力封成 MCP 工具，例如：

| MCP 工具 | 对应 REST |
|----------|-----------|
| `upload_file_base64` | `POST /v1/files` |
| `get_file_status` | `GET /v1/files/{id}` / `.../status` |
| `get_file_content` | `GET /v1/files/{id}/content` |
| `trigger_file_parse` | `POST /v1/files/{id}/parse` |
| `download_file` | `GET /v1/files/{id}/download` |
| `list_session_files` | `GET /v1/files?session_id=` |

调用方需配置 `FILE_SERVICE_BASE_URL`、`FILE_SERVICE_API_KEY`（对应 `API_KEYS`）、`FILE_SERVICE_TENANT_ID`（对应 `X-Tenant-Id`）。

## 跨智能体文件共享

### 架构决策

file service 本身**不做跨智能体的权限判断**，由上层编排服务（Agent Runtime / A2A Gateway）决策后调用以下两个原语：

```
用户/智能体 A 上传
  → reference_id 进入编排层上下文
  → 编排层决策是否允许 B 访问
  → 调用 POST /v1/files/{id}/clone，target_tenant_id=tenant-B
  → B 拿到新 reference_id，直接访问内容/下载
```

### 引用克隆

```bash
# 编排层将 tenant-A 的文件克隆给 tenant-B
curl -s -X POST http://localhost:8020/v1/files/{reference_id}/clone \
  -H "Authorization: Bearer dev-api-key" \
  -H "X-Tenant-Id: tenant-A" \
  -H "Content-Type: application/json" \
  -d '{
    "target_tenant_id": "tenant-B",
    "session_id": "sess-B",
    "agent_id": "agent-B",
    "metadata": {"purpose": "a2a-handoff"}
  }'

# 响应：新 reference_id 属于 tenant-B
# {
#   "reference_id": "<新 uuid>",
#   "source_reference_id": "<原 uuid>",
#   "content_hash": "abcd...",
#   "target_tenant_id": "tenant-B",
#   "deduplicated": true,
#   "metadata": { ... }
# }
```

**关键特性**：
- 指向同一物理 blob（不重复存储）
- 自动复用已有解析结果（不重复解析）
- 各自独立生命周期（`expires_at`、软删互不影响）
- `custom_metadata` 自动注入 `cloned_from` + `source_tenant_id` 供溯源

### 按元数据查询

```bash
# 编排层查找 某 agent 上传的全部文件
curl -s "http://localhost:8020/v1/files?agent_id=agent-A&limit=20" \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: tenant-A"

# 按文件名模糊搜索
curl -s "http://localhost:8020/v1/files?filename=contract" \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: tenant-A"
```

支持过滤参数：`session_id`、`agent_id`、`uploaded_by`、`filename`（模糊）、`content_hash`、`limit`（默认20，最大100）、`offset`。

## 推荐流程

```
用户上传（Playground / MCP upload，默认 auto_parse=true）
    → 同步：metadata 保证线完成
    → 异步：layout（及类型默认 ocr）入队
    → reference_id 写入消息上下文
    → 智能体 get_file_status
    → get_file_content(layout)     # 主路径：基础正文
    → （可选）trigger_file_parse(deep/…)  # 能力菜单
    → 业务推理（智能体侧，不在 file service）
```

### 产品化：进会话的两种路径

file service **只**管文件与 Markdown；**不**写 Agent 磁盘、不回调。

#### Path A — 与文本消息一起发送（推荐对话主路径）

```text
前端 POST /v1/files → reference_id
前端发消息：text + attachments[{reference_id, filename, doc_role}]
  或 message 内嵌 [file:uuid name]
智能体工具：get/status → content 或 materialize → workspace/uploads
```

#### Path B — 控制面预挂接（预热 / 确定性）

```text
前端 POST /v1/files → reference_id
前端/BFF POST 智能体后端 /sessions/{id}/attachments
  → 写入 workspace/uploads（可选）
```

`doc_role` 由调用方自定义。  
**不要**在 file service 内登记业务文档类型；用上传时的 `metadata` JSON 透传即可。

## 网络（Docker）

其它容器访问本服务时，加入同一 Docker 网络，容器内地址一般为 `http://agent-file-service-api-1:8000`（以 `docker compose ps` 服务名为准）。宿主机访问用 `http://127.0.0.1:8020`。

## 联调注意

- 本地默认 **lite**：`./scripts/dev.sh`（SQLite + 本地盘，无需 Postgres/MinIO）。
- API 端口固定 **8020**。
- **不要**同时启动 `docker compose api` 与宿主机 `./scripts/dev.sh` / `uvicorn :8020`；否则请求可能打到旧进程，`/download` 等路由会 404。