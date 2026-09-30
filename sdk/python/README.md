# file-service Python SDK

轻量 Python 客户端，供智能体后端 / MCP 工具调用 Agent File Service。

**要求 Python >= 3.14**（与主服务一致）。只封装 REST 协议，不做业务解读。服务端不推送解析完成事件；等待用 `wait_for_content()`（ETag 轮询）。

## 安装（不发布 PyPI）

```bash
pip install -e "/path/to/agent_file_service/sdk/python"
# 或消费方 pyproject：
# file-service-client @ file:///abs/path/to/agent_file_service/sdk/python
```

```python
from fileservice_client import FileServiceClient, get_client, init
```

发行名 `file-service-client`，导入包名 `fileservice_client`。

## 快速开始

```python
from fileservice_client import init, get_client

init(
    base_url="http://localhost:8020",
    api_key="dev-api-key",
    tenant_id="agent-system-a",
)

fs = get_client()
uploaded = fs.upload("invoice.png", session_id="sess-001", agent_id="my-agent")
ref = uploaded["reference_id"]
content = fs.wait_for_content(ref, profile="layout", timeout=120)
markdown = content.markdown  # 图片默认走视觉模型 → Markdown
```

未 `init` 时 `get_client()` / `from_env()` 读环境变量：

| 配置 | `init(...)` | 环境变量 | 默认 |
|------|-------------|----------|------|
| 服务地址 | `base_url` | `FILE_SERVICE_BASE_URL` | `http://localhost:8020` |
| API Key | `api_key` | `FILE_SERVICE_API_KEY` | 空（请求带 `Authorization: Bearer`） |
| 租户 | `tenant_id` | `FILE_SERVICE_TENANT_ID` | 空（请求带 `X-Tenant-Id`） |

## 方法面

| 方法 | REST |
|------|------|
| `health()` | `GET /health` |
| `upload(...)` | `POST /v1/files` |
| `from_url(...)` | `POST /v1/files/from-url` |
| `get_file(id)` | `GET /v1/files/{id}` |
| `list_files(...)` | `GET /v1/files` |
| `status(id)` | `GET /v1/files/{id}/status`（304 返回 `None`） |
| `content(id)` | `GET /v1/files/{id}/content` → `ContentResult` |
| `wait_for_content(id)` | 轮询 content + ETag，直到 `ready` / 失败 / 超时 |
| `parse(id, profile=..., force=...)` | `POST /v1/files/{id}/parse` |
| `custom_parse(id, prompt)` | `POST /v1/files/{id}/custom-parse` |
| `download(id)` | `GET /v1/files/{id}/download` → `bytes` |
| `delete(id)` | `DELETE /v1/files/{id}` |
| `batch_status(ids)` / `batch_content(ids)` | 批量 |
| `convert_office(url, format)` | `POST /convert-office`（file_convert 兼容） |
| `convert_markdown(md)` | `POST /convert` |
| `extract_text(url, format)` | `POST /extract-text` |
| `convert_to_images(...)` | `POST /convert-to-images` |
| `generate_excel(payload)` | `POST /generate-excel` |

`wait_for_content`：`ready=true` 返回 `ContentResult`；`status=failed` 抛 `FileServiceParseError`；超时抛 `FileServiceTimeoutError`。指数退避（默认 0.5s 起，上限 5s）。

未就绪是 HTTP 200 + `ready=false`，不是 4xx。不要把「解析中」当错误码。

服务端 `/mcp` 提供 **file_convert 兼容工具**（`convert_office_to_markdown` 等），请求须带与 REST 相同的 Bearer + `X-Tenant-Id`。文件中台主路径（upload / content / parse）的 MCP 工具由调用方用本 SDK 封装（`upload_file_base64` / `get_file_content` 等）。
