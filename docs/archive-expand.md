# 压缩包上传即解压（Archive Expand）

> 实现：`app/services/archive_service.py` · 接入：`app/services/file_service.py` · 路由 Form：`archive_password`  
> 产品表： [product-principles.md](./product-principles.md) · 配置： [configuration.md](./configuration.md) · 样例： [../sample/README.md](../sample/README.md)

## 1. 产品语义

**简单策略（当前）：** 上传 zip / tar / tar.gz 时，若可识别成员数在上限内，**同步展开为多个子 `reference_id`**，每个子文件走与普通上传相同的 metadata + auto_parse。前端 / Agent **当作多文件上传**，不维护「压缩包嵌套 badge」。

```text
upload(archive)
  → 始终存父 blob + parent reference
  → 列成员 → 过滤隐藏 / 不支持类型 / 嵌套包
  → eligible ≤ max（默认 8）→ 解压每个成员 → children[] + 识别
  → eligible > max → 只存父包（status=too_many），不建子引用
```

| 规则 | 约定 |
|------|------|
| 发给 Agent 的句柄 | **优先 `archive.children[]`**；不要只挂父 zip |
| 目录 | 不建 reference；路径写在子文件 `filename` / `archive_path` |
| 隐藏 | 忽略：`.` 段、`__MACOSX/`、`.DS_Store`、`Thumbs.db` 等 |
| 嵌套 zip/tar | **不递归**（`nested_archive` 跳过） |
| 过多 | **不拒绝上传**；`too_many` + 可 download 原包 |
| 密码 | 仅 **zip ZipCrypto**；Form `archive_password`；**不落库、不写日志** |
| AES zip / rar / 7z | 首期不支持（AES 明确失败；rar/7z 非 archive 展开） |

### 与内容寻址的关系

- **父包** hash = 整包字节（改密码 / 重压 → 新父 blob）  
- **子文件** hash = 成员明文字节（同内容跨包 dedup）  
- **目录树** 仅作过滤用的成员列表，不是 VFS  

## 2. API

### 列目录（Phase1）

```http
GET /v1/files/{reference_id}/archive/members
?archive_password=...   # 可选；zip ZipCrypto
```

返回 eligible / skipped 成员列表（path、size、skip_reason）、`status`、`encrypted`、`max_expand_files`。  
用于 `too_many` 时探查包内文件；**不**在 Phase1 做按成员异步展开。

### 上传

```http
POST /v1/files
Content-Type: multipart/form-data
Authorization: Bearer <key>
X-Tenant-Id: <tenant>

file: <binary>
session_id: optional
auto_parse: true|false
archive_password: optional   # 仅 zip
```

### 响应中的 `archive`（非压缩包为 `null`）

```json
{
  "reference_id": "<parent>",
  "archive": {
    "status": "expanded | too_many | empty | disabled | …",
    "format": "zip | tar | tar.gz | tar.bz2",
    "eligible_count": 3,
    "skipped_count": 2,
    "max_expand_files": 8,
    "message": "已解压 3 个可识别文件。",
    "encrypted": false,
    "children": [
      {
        "reference_id": "<child-uuid>",
        "filename": "docs/a.pdf",
        "content_hash": "…",
        "deduplicated": false,
        "parse_status": "pending|completed|…",
        "metadata": { "filename": "…", "mime_type": "…", "size_bytes": 0, "extra": {} }
      }
    ]
  }
}
```

| `status` | 含义 | `children` |
|----------|------|------------|
| `expanded` | 已展开并入库识别 | 1…N 子句柄 |
| `too_many` | 可识别成员超过上限 | `[]`（仅父包） |
| `empty` | 过滤后无可用成员 | `[]` |
| `disabled` | `archive_expand_enabled=false` | `[]` |

密码相关错误（HTTP **400**，`detail` 文案）：

- 加密 zip 未提供密码  
- 密码不正确  
- （AES 等）无法用 stdlib 打开时归为失败文案  

子引用 `custom_metadata`：

```json
{
  "extracted_from": "<parent_reference_id>",
  "archive_path": "docs/a.pdf"
}
```

父包：`file_kind=archive`，**不**跑 layout。

## 3. 可识别扩展名（默认）

含：pdf / office / spreadsheet / txt / md / csv / **json** / 常见图片 / 音视频。  
配置：`archive_supported_extensions`（逗号分隔；空 = 代码默认列表）。

`.json` 分类为 `file_kind=text` / `subtype=json`，可走 text layout。

## 4. 配置项

见 [configuration.md § 压缩包解压](./configuration.md)。摘要：

| 项 | 默认 |
|----|------|
| `archive_expand_enabled` | `true` |
| `archive_max_expand_files` | `8` |
| `archive_max_member_bytes` | 20 MiB |
| `archive_max_total_bytes` | 80 MiB |

## 5. 样例与联调

见 [sample/README.md](../sample/README.md)：

| 文件 | 用途 |
|------|------|
| `sample.zip` | 5× `item_*.json` + `__MACOSX` → 展开 5 个 json |
| `sample.tar` / `sample.tar.gz` | 3 小文件 |
| `sample-password.zip` | 密码 **`secret123`**（ZipCrypto） |

```bash
# 无密码 tar.gz
curl -s -X POST http://127.0.0.1:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/sample.tar.gz" | jq '.archive.status, (.archive.children|length)'

# 加密 zip
curl -s -X POST http://127.0.0.1:8020/v1/files \
  -H "Authorization: Bearer dev-api-key" -H "X-Tenant-Id: dev" \
  -F "file=@sample/sample-password.zip" \
  -F "archive_password=secret123" | jq '.archive'
```

测试：`tests/test_archive_expand.py`（含 sample.zip / 密码 / too_many）。

## 6. 调用方约定（Agent）

1. 上传后若 `archive.status=expanded` 且 `children.length>0`：  
   **pending 附件与发消息只使用 children**，勿只挂父 `reference_id`。  
2. `too_many` / `empty`：可保留父包供 download，并提示用户拆包。  
3. Demo：选 zip 时 prompt 密码；展开后走现有「附件 N」多文件 UI。

## 7. 本地 Docker 改代码不 rebuild

```bash
docker compose up -d --build
```

- 挂载 `./app`；API `uvicorn --reload`  
- Worker 需 `restart worker` 才加载新代码  
- 详见 [docker-compose.yml](../docker-compose.yml)、[DELI.md](../DELI.md)

## 8. 明确不做（本期）

- 独立 `POST …/archive/extract`、glob 挑选  
- 嵌套递归解压、目录一等引用  
- AES zip / rar / 7z  
- 父删级联子、把解压结果 materialize 进 Agent 工作区（由调用方完成）
