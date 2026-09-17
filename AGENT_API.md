# File Service 智能体对接手册

面向 **其他智能体系统 / 编排层 / BFF 的开发者**：如何调用本服务完成**文件上传与内容识别**，并做联调验收。

| 项 | 说明 |
|----|------|
| 服务 | agent-file-service（文件中台 + 文档事实层） |
| 默认入口 | `http://<host>:8020`（容器内 API 监听 8000，对外常映射 8020） |
| 协议 | HTTPS/HTTP + REST JSON；业务接口强制鉴权 |
| 本文目标 | 可独立交付联调，无需阅读仓库内 `docs/` 实现文档 |

**本服务做什么 / 不做什么**

| 做 | 不做 |
|----|------|
| 存文件、租户隔离引用、去重 | 业务字段抽取、审核结论、入库业务库 |
| 同步元数据 + 默认异步生成 **layout Markdown** | 「上传后后台跑满所有解析」 |
| 按需 OCR / deep / tables / 自定义 prompt | 跨智能体权限判定（由你方编排层决策） |
| 压缩包在限额内展开为多个子句柄 | 写你方 Agent 工作区、回调你方会话 |

---

## 1. 鉴权（所有业务 API）

每个请求必须带：

```http
Authorization: Bearer <API_KEY>
X-Tenant-Id: <TENANT_ID>
```

| 规则 | 行为 |
|------|------|
| 缺/错 Bearer | **401** |
| 缺 `X-Tenant-Id` | **401** |
| 租户不在服务端白名单（若配置了） | **403** |
| `GET /health`、`GET /metrics` | **免鉴权**（仅探活 / 内网监控） |
| UC 闸（默认关） | 人从工作台进控制台；Agent **继续只带本节 Bearer**。开闸后无 L0 的 `/v1` 须 `X-UC-Session`。见 [docs/13_identity_and_access.md](./docs/13_identity_and_access.md) |

联调时向部署方索取：`BASE_URL`、`API_KEY`、允许的 `TENANT_ID`。本地示例常用 `dev-api-key` + 自选租户（以实际环境为准）。

```bash
export BASE=http://localhost:8020
export KEY=dev-api-key
export TENANT=agent-system-a
```

---

## 2. 核心概念（必读）

| 概念 | 含义 | 你方应如何用 |
|------|------|----------------|
| `reference_id` | 租户内稳定文件句柄 | **唯一对外 ID**；写入会话附件 / 模型上下文 |
| `content_hash` | 物理内容指纹 | 仅内部/调试；**不要**当对外句柄，勿裸调 hash API |
| `profile` | 解析产物层级 | 主路径用 **`layout`**；表格另有 **`tables`**；按需 `ocr` / `deep` |
| `auto_parse` | 上传是否入队默认解析 | 默认 `true`；`false` 仅存文件 |
| `ready` | content 是否可读 | **未就绪也是 HTTP 200**，看 `ready` / `message`，勿当 4xx |

### 保证线 vs 能力菜单

```text
上传（同步）
  → L0 metadata 一定有（页数/尺寸/时长等，尽力而为）
  → 返回 reference_id

默认后台（auto_parse=true）
  → L1 layout → Markdown（智能体主消费物）
  → 表格类默认可并行 tables → 结构化 JSON

你方智能体按需
  → POST .../parse  触发 ocr / deep / tables / force 重跑
  → POST .../custom-parse  图/音/视频自定义 prompt
  → 业务推理只在你方 Agent 内完成
```

**承诺**

- 保证：上传即有元数据；默认会准备 layout（可被服务端门槛/开关跳过，见上传响应字段）。
- 不保证：deep / 高精度引擎无人触发时已完成。
- 你方负责：轮询就绪、业务解读、权限与跨租户共享策略。

---

## 3. 推荐调用循环（文件识别主路径）

```text
1. POST /v1/files                     → 得到 reference_id + metadata
2. （可选）GET  .../status            → 看 layout / tables 状态
3. GET  .../content?profile=layout    → 看 ready
      ├─ ready=false → 等待后重试（展示 message）
      └─ ready=true  → 使用 metadata.markdown 做任务
4. 不够时：
      - Excel 精确取数 → content?profile=tables
      - 扫描件/加深 → POST .../parse {"profile":"ocr"|"deep"}
      - 图音视频自定义指令 → POST .../custom-parse
5. 少数场景 → GET .../download 取原件
```

伪代码：

```python
# 1) 上传（multipart）
ref = upload_file(path)["reference_id"]

# 2) 拉 layout Markdown（未就绪也是 200）
for _ in range(30):
    c = get_content(ref, profile="layout")
    if c.get("ready"):
        markdown = c["metadata"]["markdown"]
        break
    # c["message"] 可直接给用户：排队中 / 识别中 / 失败
    sleep(1)
else:
    raise TimeoutError(c.get("message"))

# 3) 业务字段抽取、审核结论：在你方 Agent 内完成
answer = agent.reason(markdown, user_task)
```

消息里挂附件时，建议统一形如：`[file:<reference_id> <filename>]`，由工具再调 status/content。

---

## 4. 格式能力矩阵（联调预期）

| 类型 | 上传 | 同步元数据 | 默认 layout | 备注 |
|------|------|------------|-------------|------|
| PDF / docx / xlsx / txt / md | ✅ | ✅ | ✅ → Markdown | 主路径 |
| xls / xlsx / csv | ✅ | ✅ | ✅ + 默认可 **tables** JSON | 见 §7 |
| pptx / 旧 ppt | ✅ | 有限 | ✅ Markdown | 旧 ppt 需服务端 LibreOffice worker |
| 旧 doc / xls | ✅ | 有限 | ✅ | 同上，worker 转换 |
| 图片 png/jpeg/webp/gif/bmp | ✅ | 尺寸等 | ✅ 视觉模型 → Markdown | 依赖 LiteLLM；自定义用 custom-parse |
| 音频 / 视频 | ✅ | duration 等 | ✅ 转写/理解 → Markdown | 依赖模型配置；失败仍可 download |
| zip / tar / tar.gz | ✅ | 父包 | 子文件各自 layout | 可识别成员 ≤8 时展开，见 §8 |

上传**无 MIME 白名单**：能入库 ≠ 一定能识别出字；识别失败看 `status` / `content.message` / 运维侧模型与 worker。

---

## 5. REST API 一览

除特别说明外，均需 §1 鉴权头。路径前缀相对 `BASE_URL`。

### 5.1 探活

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 免鉴权。`status` 为 `ok` 或 `degraded`（依赖异常时进程仍可能 200） |
| GET | `/health?deep=true` | 重新探测依赖 |
| GET | `/metrics` | Prometheus；免鉴权，**仅内网** |

```bash
curl -s "$BASE/health" | jq .
```

### 5.2 上传

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/files` | multipart 上传（主入口） |
| POST | `/v1/files/from-url` | JSON：`{"url":"..."}` 拉取入库（服务端有 SSRF 防护） |

**`POST /v1/files` Form 字段**

| 字段 | 必填 | 说明 |
|------|------|------|
| `file` | 是 | 文件本体 |
| `session_id` | 否 | 会话 ID（常映射你方 thread/session） |
| `agent_id` | 否 | 智能体/应用 ID |
| `uploaded_by` | 否 | 上传者标识 |
| `auto_parse` | 否 | 默认 true |
| `expires_at` | 否 | 过期时间（ISO8601） |
| `metadata` | 否 | JSON 字符串，业务透传（**不要**把业务文档类型写进服务逻辑） |
| `archive_password` | 否 | 加密 zip（ZipCrypto）；**不落库** |

**成功响应（201）要点**

```json
{
  "reference_id": "uuid",
  "content_hash": "...",
  "deduplicated": false,
  "metadata": {
    "filename": "outbound.xls",
    "mime_type": "application/vnd.ms-excel",
    "size_bytes": 12345,
    "page_count": null,
    "sheet_count": 1,
    "extra": {}
  },
  "parse_status": "pending",
  "auto_parse_requested": true,
  "auto_parse_applied": true,
  "auto_parse_skip_reason": null,
  "auto_parse_message": null,
  "archive": null
}
```

| 字段 | 含义 |
|------|------|
| `auto_parse_applied=false` | 请求了自动解析但被服务端门槛/开关跳过，看 `auto_parse_skip_reason` / `message` |
| `archive` | 非压缩包为 `null`；压缩包见 §8 |

```bash
curl -s -X POST "$BASE/v1/files" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  -F "file=@./sample.pdf" \
  -F "session_id=sess-001" \
  -F "agent_id=my-agent" | jq .
```

### 5.3 状态与详情

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/files/{id}/status` | 元数据 + 各 profile 解析状态；支持 **ETag** / `If-None-Match` → 304 |
| GET | `/v1/files/{id}` | 引用详情 + `parse_results` |
| GET | `/v1/files` | 租户内列表过滤：`session_id`、`agent_id`、`uploaded_by`、`filename`、`content_hash`、`limit`、`offset` |
| GET | `/v1/sessions/{session_id}/files` | 会话内文件列表 |
| PATCH | `/v1/files/{id}` | 更新 session/agent/metadata/expires_at（默认 merge） |
| DELETE | `/v1/files/{id}` | 软删引用 |

```bash
curl -s "$BASE/v1/files/$REF/status" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" | jq .
```

`parse_results[]` 每项大致为：`profile`、`status`（`not_started` / `pending` / `processing` / `completed` / `failed`）、`parser_name` 等。

### 5.4 取识别内容（主接口）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/files/{id}/content` | 结构化内容 + 就绪 envelope |
| POST | `/v1/files/batch-content` | 批量 content（请求体含 `reference_ids` 与裁剪参数） |
| POST | `/v1/files/batch-status` | 批量 status |

**查询参数**

| 参数 | 默认 | 说明 |
|------|------|------|
| `profile` | `layout` | `layout` / `ocr` / `deep` / `tables` |
| `smart_mode` | `true` | Excel layout 去空 sheet；**对 tables 不适用** |
| `mode` | `full` | `full` 全文；`outline` 大纲（主要 layout） |
| `max_chars` | 不限 | 字符窗口；响应可含截断信息 |
| `offset` | `0` | 字符偏移（在 smart_mode / sheets 过滤之后） |
| `sheets` | — | 逗号分隔 sheet 名（layout / tables） |
| `pages` / `types` | — | 多 block 时按页 / 类型过滤 |

**就绪约定（重要）**

| 情况 | HTTP | 你方处理 |
|------|------|----------|
| 解析完成 | 200，`ready=true` | 读 `metadata.markdown`（或 tables 的 `metadata.tables_json`） |
| 排队/进行中/未开始 | 200，`ready=false` | 展示 `message`，稍后重试；**不是业务失败** |
| 解析失败 | 200，`ready=false`，`message_code` 含 failed 类 | 可 `POST .../parse` force 重试或提示用户 |
| 无权限 / 不存在 / 服务错 | 401 / 403 / 404 / 5xx | 按 HTTP 错误处理 |

```bash
# 主路径：layout Markdown
curl -s "$BASE/v1/files/$REF/content?profile=layout" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" | jq '{ready, status, message, markdown: .metadata.markdown}'

# 大文档前 8k / 大纲 / 指定 sheet
# .../content?profile=layout&max_chars=8000
# .../content?profile=layout&mode=outline
# .../content?profile=layout&sheets=Sheet1
# 需要引擎原始全量（含空白 sheet）：
# .../content?profile=layout&smart_mode=false
```

**就绪时响应骨架（示意）**

```json
{
  "schema_version": "1.0",
  "content_hash": "...",
  "ready": true,
  "status": "completed",
  "message": null,
  "message_code": null,
  "source": { "parser_name": "markitdown", "parser_version": "...", "profile": "layout" },
  "blocks": [ { "id": "...", "type": "paragraph", "page": 1, "text": "..." } ],
  "metadata": {
    "markdown": "## 标题\n\n正文…",
    "char_count": 1234
  }
}
```

**未就绪时（示意）**

```json
{
  "ready": false,
  "status": "processing",
  "message": "内容识别进行中，请稍后再试。",
  "message_code": "parse_processing",
  "metadata": {
    "ready": false,
    "markdown": null,
    "retryable": true
  }
}
```

常见 `message_code`：`parse_not_started` · `parse_pending` · `parse_processing` · `parse_failed` · `parse_timeout` · `parse_dependency_error` · `parse_unsupported_profile`。

### 5.5 触发解析（能力菜单）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/files/{id}/parse` | 触发解析；**202** |

请求体：

```json
{ "profile": "layout", "force": false }
```

| `profile` | 用途 |
|-----------|------|
| `layout` | 基础 Markdown（默认保证线；可 force 重跑） |
| `tables` | 表格 headers+rows JSON |
| `ocr` | OCR 类加料 |
| `deep` | 更深理解（如图片视觉加深） |

`force=true`：跳过缓存强制重解析。触发后仍用 `GET .../content?profile=...` 取结果。

```bash
curl -s -X POST "$BASE/v1/files/$REF/parse" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Content-Type: application/json" \
  -d '{"profile":"deep","force":false}' | jq .
```

### 5.6 自定义解析（图 / 音 / 视频）

与共享 `layout` 缓存隔离，每次任务独立 `job_id`。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/files/{id}/custom-parse` | 提交 `prompt` |
| GET | `/v1/files/{id}/custom-parse` | 列出任务 |
| GET | `/v1/files/{id}/custom-parse/{job_id}` | 单次结果（含 `result_text`） |

```bash
curl -s -X POST "$BASE/v1/files/$REF/custom-parse" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"提取图中所有发票号码与金额，按 JSON 列出"}' | jq .

# 轮询 job
curl -s "$BASE/v1/files/$REF/custom-parse/$JOB_ID" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" | jq '{status, result_text, error, message}'
```

### 5.7 下载原件

```bash
curl -s -L -o out.bin \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  "$BASE/v1/files/$REF/download"
```

---

## 6. Excel / 表格：layout 与 tables

| 需求 | 调用 |
|------|------|
| 给 LLM 读表意 | `GET .../content?profile=layout` → `metadata.markdown`（默认去空 sheet） |
| 给代码精确取数 | `GET .../content?profile=tables` → `metadata.tables_json` |

- `tables` 与 layout **独立缓存**；默认上传后可与 layout **并行**准备（以环境配置为准）。
- 也可：`POST .../parse` + `{"profile":"tables"}`。
- 过滤 sheet：`?profile=tables&sheets=应用系统`。

`tables_json` schema 标识：`spreadsheet_tables@v1`（sheets → headers + rows）。

---

## 7. 压缩包（zip / tar / tar.gz）

| 上传结果 `archive.status` | 你方应如何挂附件 |
|---------------------------|------------------|
| `expanded` | **只把 `children[]` 当多个文件**（各有 `reference_id`），与多选上传相同 |
| `too_many` | 成员过多未展开；可 `GET .../archive/members` 列目录；父包可 download |
| `empty` | 无可识别成员；仅父包 |
| 加密 zip 无/错密码 | HTTP **400** |

```bash
# 加密 zip（密码由部署方/样例约定，示例 secret123）
curl -s -X POST "$BASE/v1/files" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: $TENANT" \
  -F "file=@sample-password.zip" \
  -F "archive_password=secret123" | jq '.archive'
```

子文件默认各自 auto_parse；**父包不跑 layout**。

---

## 8. 跨智能体共享（编排层）

本服务**不做**「A 能否把文件给 B」的业务鉴权。编排层决策后：

1. 用 A 的 `Authorization` + `X-Tenant-Id: tenant-A`
2. `POST /v1/files/{id}/clone`

```bash
curl -s -X POST "$BASE/v1/files/$REF/clone" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Tenant-Id: tenant-A" \
  -H "Content-Type: application/json" \
  -d '{
    "target_tenant_id": "tenant-B",
    "session_id": "sess-B",
    "agent_id": "agent-B",
    "metadata": {"purpose": "a2a-handoff"}
  }' | jq .
```

新 `reference_id` 属于 B：同 blob、复用解析结果、生命周期独立。

---

## 9. 联调验收清单（建议对方按序过一遍）

环境变量见 §1。将 `$FILE` 换成你方测试文件。

### 9.1 鉴权

```bash
# 应 401
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/v1/files"

# 应成功探活
curl -s "$BASE/health" | jq .status
```

### 9.2 上传 → 识别 → Markdown（主路径）

```bash
REF=$(curl -s -X POST "$BASE/v1/files" \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  -F "file=@$FILE" | jq -r .reference_id)
echo "reference_id=$REF"

# 轮询 status（layout completed）
curl -s "$BASE/v1/files/$REF/status" \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" | jq .

# 取 content
curl -s "$BASE/v1/files/$REF/content?profile=layout" \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  | jq '{ready, status, message_code, has_md: (.metadata.markdown!=null)}'
```

| 期望 | 说明 |
|------|------|
| 上传 201 | 有 `reference_id`、`metadata` |
| 一段时间后 layout completed | 文本/Office/PDF 通常较快；图/音视频依赖模型 |
| content `ready=true` | `metadata.markdown` 非空字符串 |

### 9.3 表格（若测 xls/xlsx）

```bash
curl -s "$BASE/v1/files/$REF/content?profile=tables" \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  | jq '{ready, tables_json: .metadata.tables_json}'
```

### 9.4 按需 parse

```bash
curl -s -X POST "$BASE/v1/files/$REF/parse" \
  -H "Authorization: Bearer $KEY" -H "X-Tenant-Id: $TENANT" \
  -H "Content-Type: application/json" \
  -d '{"profile":"layout","force":true}' | jq .
```

### 9.5 压缩包（可选）

展开后 `archive.children | length >= 1`，且对每个 child 可走 §9.2。

---

## 10. 常见 HTTP / 产品错误

| 现象 | 含义 | 建议 |
|------|------|------|
| 401 | 鉴权失败 | 检查 Bearer / Tenant 头 |
| 403 | 租户不允许 | 找部署方加白名单或换 tenant |
| 404 | 引用不存在或非本租户 | 核对 `reference_id` 与 `X-Tenant-Id` |
| 413 | 超过上传大小上限 | 压缩或提高服务端 `max_upload_bytes` |
| 429 | 配额限流 | 降速 / 找运维调 quota |
| content `ready=false` 且 processing | 正常异步 | 退避轮询（建议 0.5–2s，设超时） |
| content failed / dependency | 模型或 worker 依赖 | 看 `message`；图音视频确认 LiteLLM；旧 Office 确认 worker |
| health `degraded` | 进程活、依赖异常 | 解析可能失败，找运维查依赖 |

---

## 11. 集成设计建议

1. **主路径只消费 layout Markdown**，业务 schema 在你方 Agent 内抽取。  
2. **统一处理 `ready=false`**：UI / 工具返回 `message`，不要把 200 当失败，也不要误用 409。  
3. **轮询优先 status/content + ETag**，避免无脑 force 重解析。  
4. **大文档用 `max_chars` / `mode=outline` / `sheets`** 控制进模型的上下文。  
5. **压缩包 expanded 时只挂 children**，避免把父 zip 当正文塞给模型。  
6. **跨租户只用 clone**，不要共享对方的 `reference_id` 跨 tenant 直读。  
7. 上传 `metadata` 只做业务透传；文档类型/角色（如招采、外报）放你方会话或该 JSON 字段。

---

## 12. 与部署方交接清单

向运维 / 交付方确认：

| 项 | 示例 |
|----|------|
| API 入口 | `https://files.example.com` 或 `http://host:8020` |
| API_KEY | 专用联调 key（非生产共用） |
| 租户 ID | `tenant-your-system`（是否在白名单） |
| 上传大小上限 | 默认常见约 20 MiB（以环境为准） |
| 图/音/视频是否可用 | 依赖 LiteLLM 与模型名是否已配 |
| 旧式 doc/ppt/xls | 是否部署了带 LibreOffice 的 worker |
| 网络 | 你方服务是否能访问 file service（Docker 网络 / 网关） |

运维部署与验收命令见同目录 **[DELI.md](./DELI.md)**（面向实施，不替代本文接口约定）。

---

## 13. 文档边界

| 文档 | 读者 |
|------|------|
| **本文 `AGENT_API.md`** | 其他智能体系统 / 编排对接开发者 |
| [DELI.md](./DELI.md) | 运维部署与环境验收 |
| [README.md](./README.md)、`docs/*` | 本服务内部开发与实现细节 |

实现细节、配置项、架构决策以仓库 `docs/` 为准；**对接联调以本文为准**。若行为与本文冲突，以实际运行环境响应与部署方说明为准，并反馈维护方更新本文。
