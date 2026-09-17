# 文件智能处理服务 — 文档索引

本目录记录从 [design.md](../design.md) 拆解出的产品约定、实现计划、架构决策与当前进度。

## 文档地图

| 文档 | 说明 |
|------|------|
| **[product-principles.md](./product-principles.md)** | **产品原则（必读）**：定位、保证线×能力菜单、结构/业务切分、Agent 契约 |
| [metadata-extraction.md](./metadata-extraction.md) | 分类型元数据字段契约与扩展方式 |
| [processing-factory.md](./processing-factory.md) | 处理工厂：类型/子类型/层级路由 |
| [agent-integration.md](./agent-integration.md) | 智能体 REST / MCP 集成与使用心智 |
| [architecture-notes.md](./architecture-notes.md) | 架构理解、流式上传、GC 竞态等取舍 |
| **[frontend-dev-console.md](./frontend-dev-console.md)** | **开发控制台**（skillmesh 风格 MPA、config 鉴权、指标页） |
| [configuration.md](./configuration.md) | 环境变量与本地开发配置 |
| **[13_identity_and_access.md](./13_identity_and_access.md)** | **UC 工作台跳转鉴权**（`?token=` → 本地 `X-UC-Session`；非 OAuth） |
| [implementation-status.md](./implementation-status.md) | **实现状态看板**（模块完成度、测试、变更日志） |
| [implementation-plan.md](./implementation-plan.md) | 历史里程碑索引（已归档，不再维护任务表） |
| [legacy-office.md](./legacy-office.md) | 旧式 doc/ppt/xls → LibreOffice → OOXML |
| **[archive-expand.md](./archive-expand.md)** | **压缩包上传即解压**（zip/tar、密码、children 句柄、样例） |

总设计：[../design.md](../design.md) · 仓库入口：[../README.md](../README.md) · 样例：[../sample/README.md](../sample/README.md)

## 讨论结论摘要

### 2026-07-14

1. **定位**：文件管理底座 + 文档事实层；不是网盘，不是业务 Agent。  
2. **元数据**：按类型分流的同步事实提取（保证线 L0），已实现 PDF/Office/表格/文本/图片。  
   旧式 **doc/ppt/xls** 的 layout 规范化见 [legacy-office.md](./legacy-office.md)。  
3. **触发模型**：保证线自动（metadata + 默认 layout）；能力菜单按需（deep 等工具触发）——避免「狂解析」与「全靠 Agent 记」两个极端。  
4. **划分边界**：只做结构切分，不做业务语义切分。  

### 2026-07-15

5. **智能模式 `smart_mode`（默认 true）**：`GET /content` 对表格丢弃空 sheet；缓存保留原文。  
6. **格式边界**：上传无 MIME 白名单；图片/Office/表格一等；旧式 doc/ppt/xls 见 [legacy-office.md](./legacy-office.md)；音视频 layout 依赖 LiteLLM。权威表见 [product-principles.md](./product-principles.md)。

### 2026-07-24

7. **压缩包上传即解压**：zip / tar / tar.gz 在可识别成员 ≤8 时展开为多个子 `reference_id`（过滤隐藏/不支持类型）；过多只存父包。zip 支持 Form `archive_password`（ZipCrypto）。调用方按多文件挂附件。见 [archive-expand.md](./archive-expand.md)。  
8. **本地 Docker 热更**：`docker-compose.yml` 挂载 `./app` + API `--reload`，免频繁 rebuild。

### 2026-07-27（Phase1 P0–P2）

9. **读路径**：`GET /content` 支持 `mode` / `max_chars` / `offset` / `sheets`；layout 多 block（parser 0.2.0）；status/content **ETag**。  
10. **能力**：`profile=ocr` 正式 API；**PATCH** 引用；**batch-status / batch-content**；**from-url**（SSRF）；**archive/members**。  
11. **运维 / 安全**：`API_KEY_TENANT_MAP` + audit；租户配额 429；`GET /metrics`。  
12. **媒体 L0**：音视频 duration 等元数据；图片默认 layout=视觉模型（与 product-principles 一致）。

### 2026-08-20

16. **UC 统一工作台鉴权**：`?token=` → `token-auth` → 本地 HMAC 票；默认关闸。见 [13_identity_and_access.md](./13_identity_and_access.md)。

### 2026-08-07

13. **正文识别后端扩展 `vision-pages`**：PDF/Office/表格转页图后经 LiteLLM 多模态识别为 Markdown；可配置 `vision_pages_concurrency` 并行识别并按页序拼接。`layout_parser_name` 在产品语义上为正文识别后端选择（API 槽位仍为 `profile=layout`）。见 [processing-factory.md](./processing-factory.md) · [configuration.md](./configuration.md)。

15. **开发控制台**：`web/` skillmesh 风格 MPA（大盘 / 文件 / 上传 / 会话 / 指标 / 调试）；`GET /metrics` 补强 downloads/content/custom-parse/http 等 counter。见 [frontend-dev-console.md](./frontend-dev-console.md)。

### 2026-07-28

13. **文档对齐代码**：修正 processing-factory / metadata-extraction 中「图片未接视觉」「pptx/音频为 unknown」等过时描述；测试清单与全量 **195 passed** 对齐。

### 2026-08-03

14. **`profile=tables` 文档与用例闭环**：spreadsheet 默认可并行 headers+rows JSON（`also_tables`）；权威 docs / README / design 与代码一致；API 用例补齐。

## 项目状态（摘要）

**M1–M7 + Phase1 P0–P2 已落地**。进度与测试表 → [implementation-status.md](./implementation-status.md)。

## 快速命令

```bash
# 本地 lite（推荐）：SQLite + 本地盘 + 单进程
pip install -e ".[dev,parsers]"
./scripts/dev.sh

# 或 Docker 本地开发（lite + 热更）
docker compose up -d --build

uv run pytest -q
# 或：python3 -m pytest tests/ -v
# 压缩包相关
uv run pytest tests/test_archive_expand.py -v
```
