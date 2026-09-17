# 旧版 Office 与金山 WPS 规范化

国企场景仍大量使用二进制旧式 Office 与金山 WPS 原生格式。MarkItDown 稳定支持 **docx / pptx / xlsx**；对下列格式在 layout 前经 **LibreOffice** 转为 OOXML，再解析为 Markdown。

| 源格式 | 目标 | 说明 |
|--------|------|------|
| `.doc` / `.ppt` / `.xls` | docx / pptx / xlsx | 旧版 MS Office |
| `.wps` / `.wpt` | docx | 金山 WPS 文字（**尽力而为**） |
| `.et` / `.ett` | xlsx | 金山 WPS 表格（**尽力而为**） |
| `.dps` / `.dpt` | pptx | 金山 WPS 演示（**尽力而为**） |

与 [file_convert](../../common_services/file_convert) 的 `convert-office` 路径对齐，但身份模型归 file service。

### WPS 注意点

1. **Best-effort**：LibreOffice 对 `.et` / `.dps` / 部分新版 `.wps` **非一等官方支持**；复杂版式可能丢失或转换失败。失败时上传与句柄仍成功，layout 写入可读错误。
2. **MIME 冲突**：Python/`mimetypes` 常把 `.wps` 猜成 `application/vnd.ms-works`（**微软 Works**）。本服务分类以**扩展名优先**，且**不会**仅凭 `vnd.ms-works` 判为金山 WPS。
3. 明确 MIME hint（调用方可传）：`application/wps-office.wps` / `.et` / `.dps` 等。

## 存储决策：原文件 vs 转换后文件

| 对象 | 是否存储 | 形态 | 智能体可见 |
|------|----------|------|------------|
| **用户上传的原文件** | ✅ 必须 | Blob（content_hash）+ FileReference | ✅ `file_id` / download 即原件 |
| **转换后的 docx/pptx/xlsx** | ✅ 默认缓存 | **派生产物** `derivatives/…` | ❌ 不建第二引用 |
| **layout Markdown** | ✅ | `parse-results/…` | ✅ `get_content` |

### 为何不把 OOXML 做成第二个「用户文件」？

1. **身份唯一**：会话里仍是「用户传的那份 `.doc`」，避免「到底用哪个 id」的歧义。  
2. **审计与举证**：下载/哈希应对齐**原始字节**；规范化是内部实现细节。  
3. **去重键稳定**：`content_hash` 永远是原件；转换结果随引擎版本可变。  
4. **成本**：LibreOffice 贵，把 OOXML 缓存在 `derivatives/` 可避免每次 re-parse 重转；但不进入 reference 列表。

### 何时可以「晋升」为正式文件？

仅当产品明确需要「下载规范化后的 xlsx」时，再提供能力菜单（例如 `export normalized`），**显式**生成新 `FileReference`。默认不做。

## 处理流程

```text
上传 notice.doc
  → blob(content_hash) + reference_id   # 只存原件
  → metadata（legacy 标记）
  → layout：
       detect legacy → LibreOffice → normalized.docx
       → 可选写入 derivatives/{hash}/legacy-office/{ver}/normalized.docx
       → MarkItDown → Markdown → parse-results
  → Agent：get_content(layout) 读 Markdown；download 仍是 .doc
```

## 配置

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `legacy_office_enabled` | `true` | 总开关 |
| `legacy_office_cache_enabled` | `true` | 是否缓存 OOXML 派生产物 |
| `legacy_office_timeout` | `300` | LibreOffice 超时（秒） |
| `legacy_office_binary` | `""` | soffice 路径；空则 PATH 查找 |
| `legacy_office_converter_version` | `0.1.0` | 缓存键版本；升级可失效旧缓存 |

依赖：宿主机或镜像中安装 **LibreOffice**（`soffice` / `libreoffice`）。未安装时，对 `.doc/.ppt/.xls` 与 WPS（`.wps/.et/.dps` 等）的 layout 会失败并写入可读错误（上传与句柄仍成功）。

### Docker 镜像（仅 worker）

LibreOffice **只装在 worker 镜像**（`Dockerfile` target `worker`），api 镜像保持轻量：

- `libreoffice-writer` / `libreoffice-calc` / `libreoffice-impress`
- `libreoffice-java-common` + `default-jre-headless`
- `fonts-wqy-zenhei`、`fonts-dejavu-core`（中文/常见西文）

layout / 旧格式转换在 **worker** 执行；api 只负责上传与投递 Celery 任务。两者共享 volume `/data`（lite 下含 SQLite + blobs + derivatives）。

```bash
./build.sh --native --skip-scan   # 构建 api + worker，并打 :local
docker compose up -d --force-recreate
# 或指定版本：
# set -a && source last_image_tag.txt && set +a && docker compose up -d --force-recreate
```

`build.sh` 只把 LibreOffice 打进 **worker** target；`last_image_tag.txt` 为可 source 的 `IMAGE_TAG` / `API_IMAGE` / `WORKER_IMAGE`。详见根目录 [README.md](../README.md)「标准镜像构建」。

worker 镜像体积会明显增大（LibreOffice 通常数百 MB 级），属国企旧格式保证线的必要成本；api 镜像不承担该成本。

## 可观测字段

layout 完成后：

- `metadata.legacy_converted` / `summary.legacy_converted`
- `legacy_source_format` / `legacy_target_format`
- `legacy_cache_hit`
- `legacy_converter` / `legacy_converter_version`

## GC

原 blob `ref_count=0` 被 GC 时，删除：

- blob 对象  
- parse-results  
- **legacy-office derivatives**（`purge_legacy_derivatives`）

## 与 file_convert 迁移关系

| file_convert | file service |
|--------------|--------------|
| `POST /convert-office` + `file_url` | 上传/import → layout（内含 legacy 转换） |
| 返回 `markdown_content` | `GET .../content?profile=layout` |
| 自建 hash 缓存 markdown | parse_results + 可选 derivatives OOXML |

读路径迁移时，应依赖本能力而非长期旁路 convert 服务。
