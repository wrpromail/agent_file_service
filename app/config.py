"""应用配置：config.yaml（服务行为）+ 环境变量 / .env（密钥与部署连接）。

优先级（高 → 低）：
1. 进程环境变量
2. `.env`
3. `config.yaml`（路径见 CONFIG_PATH，默认项目根 / 工作目录下 `config.yaml`）
4. 代码默认值
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# auto_parse YAML 中 kind → Settings 扁平字段前缀
_AUTO_PARSE_KINDS = (
    "pdf",
    "spreadsheet",
    "office",
    "text",
    "image",
    "audio",
    "video",
    "unknown",
)


def resolve_config_yaml_path() -> Path | None:
    """定位 config.yaml；不存在则返回 None（仅用 env/默认值）。"""
    raw = (os.environ.get("CONFIG_PATH") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    # 工作目录 / 包上级（仓库根）
    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(Path(__file__).resolve().parent.parent / "config.yaml")
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def _flatten_yaml_config(data: dict[str, Any]) -> dict[str, Any]:
    """将 config.yaml 结构扁平为 Settings 字段名。"""
    flat: dict[str, Any] = {}
    nested_auto = data.get("auto_parse")
    for key, value in data.items():
        if key == "auto_parse" and isinstance(value, dict):
            continue
        if isinstance(value, dict):
            # 忽略未声明的嵌套块，避免误吞
            continue
        flat[key] = value

    if isinstance(nested_auto, dict):
        for kind in _AUTO_PARSE_KINDS:
            section = nested_auto.get(kind)
            if not isinstance(section, dict):
                continue
            prefix = f"auto_parse_{kind}"
            if "enabled" in section:
                flat[f"{prefix}_enabled"] = section["enabled"]
            if "max_pages" in section:
                flat[f"{prefix}_max_pages"] = section["max_pages"]
            if "max_bytes" in section:
                flat[f"{prefix}_max_bytes"] = section["max_bytes"]
            if "max_sheets" in section:
                flat[f"{prefix}_max_sheets"] = section["max_sheets"]
            if "profile" in section:
                flat[f"{prefix}_profile"] = section["profile"]
            # spreadsheet-only extras
            if kind == "spreadsheet":
                if "also_tables" in section:
                    flat["auto_parse_spreadsheet_also_tables"] = section["also_tables"]
                if "tables_max_rows" in section:
                    flat["auto_parse_spreadsheet_tables_max_rows"] = section["tables_max_rows"]
                if "tables_max_cols" in section:
                    flat["auto_parse_spreadsheet_tables_max_cols"] = section["tables_max_cols"]
    return flat


def load_yaml_config_dict(path: Path | None = None) -> dict[str, Any]:
    path = path or resolve_config_yaml_path()
    if path is None:
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PyYAML is required to load config.yaml; install with: pip install pyyaml"
        ) from exc
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml root must be a mapping: {path}")
    return _flatten_yaml_config(raw)


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """从 config.yaml 注入非密钥服务配置。"""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = load_yaml_config_dict()

    def get_field_value(self, field, field_name: str):  # noqa: ANN001
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # 只返回 Settings 已声明的字段
        field_names = set(self.settings_cls.model_fields)
        return {k: v for k, v in self._data.items() if k in field_names}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "file-service"
    debug: bool = False

    # 开发控制台静态 MPA（web/）。本地/测试默认开；交付 compose 默认关（DEV_CONSOLE_ENABLED=false）。
    # 关闭后不挂载 HTML /shared；api 镜像仍可含 web/ 资产以便按需打开。
    dev_console_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("DEV_CONSOLE_ENABLED", "dev_console_enabled"),
    )

    # API 启动时探测外部依赖（LiteLLM / DB / Celery 时的 Valkey / MinerU 等）
    startup_dependency_check: bool = True
    startup_dependency_check_timeout: float = 5.0
    # true = 关键依赖失败则拒绝启动；false = 仅打日志，/health 标 degraded
    startup_dependency_check_strict: bool = False

    # lite (default): SQLite + local filesystem (Docker: api+worker 共享 /data)
    # full: PostgreSQL + S3/MinIO + Valkey/Celery
    # 部署相关优先由环境变量 / compose 注入
    deploy_mode: str = "lite"

    # Default lite paths are relative to process cwd (./data). Docker sets absolute /data.
    database_url: str = "sqlite:///./data/db/file_service.db"
    storage_backend: str = "local"  # local | s3 | memory
    local_storage_path: str = "./data"

    valkey_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "file-service"
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False

    # 密钥：仅环境变量 / .env（逗号分隔多个）。空 = 启动时自动生成 ephemeral key 并打日志。
    # Authorization 鉴权不可关闭（dev-security-standard §5.0）。
    # 兼容别名 API_TOKEN / STATIC_TOKEN = 单 key。
    api_keys: str = Field(
        default="",
        validation_alias=AliasChoices("API_KEYS", "API_TOKEN", "STATIC_TOKEN"),
    )

    # 可选：API key → 租户绑定。空 = 不绑定（仅受 allowed_tenant_ids 约束）。
    # 格式：key1:tenant-a|tenant-b,key2:*   （* = 该 key 可访问任意租户）
    api_key_tenant_map: str = Field(
        default="",
        validation_alias=AliasChoices("API_KEY_TENANT_MAP"),
    )

    # 可选租户白名单（逗号分隔）。空 = 调用方自报任意非空 X-Tenant-Id；
    # 非空 = 仅列表内 tenant 可访问（含 clone 的 target_tenant_id）。
    allowed_tenant_ids: str = Field(
        default="",
        validation_alias=AliasChoices("ALLOWED_TENANT_IDS"),
    )

    # 批量 API 单次最多 id 数
    batch_max_ids: int = 20

    # 租户配额（0 = 不限制）
    quota_max_files_per_tenant: int = 0
    quota_max_bytes_per_tenant: int = 0
    quota_max_concurrent_parses_per_tenant: int = 0

    # from-url 上传
    from_url_enabled: bool = True
    from_url_timeout: float = 60.0
    # 逗号分隔 host 白名单；空 = 仅 SSRF 规则（禁私网）
    from_url_allowed_hosts: str = ""

    parse_sync: bool = False
    # 异步任务后端：local = 进程内 BackgroundTasks（本机单进程 dev）；
    # celery = 投递到 worker 容器（Docker 默认）。parse_sync=true 时忽略此项。
    parse_task_backend: str = "local"
    # 单文件上传硬上限（字节）；0 = 不限制。超出返回 HTTP 413（与 auto_parse 门槛无关）
    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MiB

    # ── 压缩包上传即解压（zip / tar / tar.gz）──────────────────────────────
    # 可识别成员 ≤ max 则展开为子 reference 并识别；过多只存父包。
    archive_expand_enabled: bool = True
    archive_max_expand_files: int = 8
    # 单成员 / 合计未压缩上限（zip bomb）；0 = 不限制（仍受 max_upload 与成员过滤约束）
    archive_max_member_bytes: int = 20 * 1024 * 1024
    archive_max_total_bytes: int = 80 * 1024 * 1024
    # 可识别扩展名（逗号分隔，可带点）；空 = 内置默认列表
    archive_supported_extensions: str = (
        ".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.txt,.md,.csv,.json,"
        ".png,.jpg,.jpeg,.webp,.gif,.bmp,"
        ".wav,.mp3,.m4a,.ogg,.flac,.mp4,.webm,.mov"
    )

    # 正文识别后端（历史字段名 layout_parser_*；结果写入 profile=layout = 基础正文 Markdown）
    # markitdown | simple | mineru | vision-pages
    layout_parser_name: str = "markitdown"
    layout_parser_version: str = "0.2.0"

    # 旧版 Office（doc/ppt/xls）→ OOXML（LibreOffice），供 layout 使用
    # 转换结果默认缓存为派生产物，不建第二 FileReference（见 docs/legacy-office.md）
    legacy_office_enabled: bool = True
    legacy_office_cache_enabled: bool = True
    legacy_office_timeout: int = 300
    legacy_office_binary: str = ""  # 空则 PATH 上找 soffice / libreoffice
    legacy_office_converter_version: str = "0.1.0"

    mineru_api_token: str = ""
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_poll_interval: int = 5
    mineru_timeout: int = 900
    mineru_work_dir: str = "/tmp/file-service-mineru"
    # MinerU 出站重试：0 = 继承 outbound_http_*；默认 5 与历史行为一致
    mineru_max_attempts: int = 5
    mineru_backoff_base: float = 0.0  # 0 = 继承
    mineru_backoff_max: float = 0.0
    mineru_rate_limit_wait: float = 0.0

    # ── vision-pages：正文识别后端（页图 + 多模态；PDF / Office / 表格）──
    # 启用：layout_parser_name=vision-pages（opt-in；不是版面 layout 引擎）
    vision_pages_model: str = "kimi-k2.6:cloud"
    vision_pages_parser_name: str = "vision-pages"
    vision_pages_parser_version: str = "0.1.0"
    vision_pages_max_pages: int = 20
    vision_pages_render_scale: float = 2.0  # pypdfium2 render scale（≈ 144 dpi at 1.0）
    vision_pages_max_edge_px: int = 1600  # 送模型前最长边上限
    vision_pages_timeout: int = 180  # 单页 LiteLLM 超时
    vision_pages_work_dir: str = "/tmp/file-service-vision-pages"
    vision_pages_fail_fast: bool = False  # True=单页失败整单失败
    # 多页识别并发：>1 时用线程池并行调用多模态；1 或更小 = 串行
    vision_pages_concurrency: int = 3
    vision_pages_page_prompt: str = (
        "请将本页图像中的可见文本还原为结构清晰的 Markdown 文档"
        "（保留标题、列表、表格与阅读顺序）。仅描述本页内容，不要编造不存在的文字或数字。"
        "无文字则用一两句客观描述画面。直接输出 Markdown 正文，不要前言后语。"
    )

    # ── 外部 HTTP 出站（LiteLLM / MinerU 等共用默认；见 app/integrations/http）──
    # max_attempts：总尝试次数（含首次）。1 = 不重试；3 = 最多再试 2 次
    outbound_http_max_attempts: int = 3
    outbound_http_backoff_base: float = 1.0
    outbound_http_backoff_max: float = 30.0
    outbound_http_rate_limit_wait: float = 65.0  # 429 时等待秒数（再加 jitter）

    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = "sk-dev-key"
    # LiteLLM 重试：0 = 继承 outbound_http_max_attempts
    litellm_max_attempts: int = 0
    litellm_backoff_base: float = 0.0
    litellm_backoff_max: float = 0.0
    litellm_rate_limit_wait: float = 0.0
    vision_model: str = "qwen3.5:cloud"
    vision_parser_name: str = "qwen-vision"
    vision_parser_version: str = "0.1.0"
    vision_timeout: int = 120
    # 多模态默认提示：引导模型输出 Markdown，契合媒体中文本结构（标题/表/列表/分节）
    vision_default_prompt: str = (
        "请将图片中的可见文本还原为结构清晰的 Markdown 文档"
        "（保留标题、列表、表格与阅读顺序）；必要时用一两句话客观描述画面。"
        "无文字则仅描述画面。"
    )

    # 音频 layout / custom-parse（经 LiteLLM；模型需支持 input_audio）
    # 网关模型名示例：gemini-flash-3.5（非 gemini-3.5-flash）
    audio_model: str = "gemini-flash-3.5"
    audio_parser_name: str = "litellm-audio"
    audio_parser_version: str = "0.1.0"
    audio_timeout: int = 180
    audio_default_prompt: str = (
        "请将音频内容整理为 Markdown 文档：\n"
        "## 概要\n（一两句主题；无明显语音可省略本节）\n"
        "## 转写\n完整转写语音；可按说话人或自然段落分段。"
        "若无明显语音请明确说明。"
    )

    # 视频 layout / custom-parse（经 LiteLLM；模型需支持 video/file 多模态）
    # 本地测试：gemini-flash-3.5；内网后续可切 qwen3-omni 等
    video_model: str = "gemini-flash-3.5"
    video_parser_name: str = "litellm-video"
    video_parser_version: str = "0.1.0"
    video_timeout: int = 300
    video_default_prompt: str = (
        "请将视频内容整理为 Markdown 文档：\n"
        "## 概述\n画面与情节简要说明\n"
        "## 画面要点\n关键视觉信息（列表）\n"
        "## 语音与字幕\n转写或概括语音/字幕中的文本；可按时间分段\n"
        "若无明显有效内容请明确说明。"
    )

    # ── 自动解析：分类型开关 + 门槛 + profile（0 = 不限制）────────────────
    # 上传 auto_parse=true 时仍受本策略约束；显式 POST /parse 不受限。
    auto_parse_pdf_enabled: bool = True
    auto_parse_pdf_profile: str = "layout"
    auto_parse_pdf_max_pages: int = 100
    auto_parse_pdf_max_bytes: int = 30 * 1024 * 1024

    auto_parse_spreadsheet_enabled: bool = True
    auto_parse_spreadsheet_profile: str = "layout"
    auto_parse_spreadsheet_max_pages: int = 0  # 未用；sheet 数见 max_sheets
    auto_parse_spreadsheet_max_sheets: int = 50
    auto_parse_spreadsheet_max_bytes: int = 20 * 1024 * 1024
    # Extra profile tables (headers+rows JSON) scheduled after layout for spreadsheets.
    auto_parse_spreadsheet_also_tables: bool = True
    auto_parse_spreadsheet_tables_max_rows: int = 5000
    auto_parse_spreadsheet_tables_max_cols: int = 64

    auto_parse_office_enabled: bool = True
    auto_parse_office_profile: str = "layout"
    auto_parse_office_max_pages: int = 0
    auto_parse_office_max_bytes: int = 20 * 1024 * 1024

    auto_parse_text_enabled: bool = True
    auto_parse_text_profile: str = "layout"
    auto_parse_text_max_pages: int = 0
    auto_parse_text_max_bytes: int = 5 * 1024 * 1024

    # 图片默认开启自动视觉识别（写入 layout，供 Agent 消费；引擎为 vision）
    auto_parse_image_enabled: bool = True
    auto_parse_image_profile: str = "layout"
    auto_parse_image_max_pages: int = 0
    auto_parse_image_max_bytes: int = 10 * 1024 * 1024

    # 音频默认自动 layout 转写（Agent 读 layout；引擎为 audio_model）
    auto_parse_audio_enabled: bool = True
    auto_parse_audio_profile: str = "layout"
    auto_parse_audio_max_pages: int = 0
    auto_parse_audio_max_bytes: int = 50 * 1024 * 1024

    # 视频默认自动 layout 理解（Agent 读 layout；引擎为 video_model）
    auto_parse_video_enabled: bool = True
    auto_parse_video_profile: str = "layout"
    auto_parse_video_max_pages: int = 0
    auto_parse_video_max_bytes: int = 100 * 1024 * 1024

    # pptx 等 unknown：尽力 MarkItDown
    auto_parse_unknown_enabled: bool = True
    auto_parse_unknown_profile: str = "layout"
    auto_parse_unknown_max_pages: int = 0
    auto_parse_unknown_max_bytes: int = 20 * 1024 * 1024

    # API lifespan 定时 GC 开关（非 Celery Beat；命名保留兼容）
    gc_beat_enabled: bool = True
    gc_expire_interval_seconds: int = 3600
    gc_blob_interval_seconds: int = 86400
    # 卡住的 processing / purging 看门狗（秒）
    gc_processing_reclaim_seconds: int = 6 * 3600
    # SQLite 锁等待（毫秒）
    sqlite_busy_timeout_ms: int = 5000
    # Postgres 连接池
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    # Celery 单任务硬超时（秒）；0 = 不设
    celery_task_time_limit: int = 3600
    # 单次 expire / blob GC 批大小
    gc_batch_size: int = 100

    @field_validator("deploy_mode")
    @classmethod
    def _normalize_deploy_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"lite", "full"}:
            raise ValueError("deploy_mode must be 'lite' or 'full'")
        return mode

    @field_validator("storage_backend")
    @classmethod
    def _normalize_storage_backend(cls, value: str) -> str:
        backend = value.strip().lower()
        if backend not in {"local", "s3", "memory"}:
            raise ValueError("storage_backend must be 'local', 's3', or 'memory'")
        return backend

    @field_validator(
        "auto_parse_pdf_profile",
        "auto_parse_spreadsheet_profile",
        "auto_parse_office_profile",
        "auto_parse_text_profile",
        "auto_parse_image_profile",
        "auto_parse_audio_profile",
        "auto_parse_video_profile",
        "auto_parse_unknown_profile",
    )
    @classmethod
    def _normalize_auto_parse_profile(cls, value: str) -> str:
        profile = (value or "layout").strip().lower()
        if profile not in {"layout", "deep", "ocr"}:
            raise ValueError("auto_parse profile must be one of: layout, deep, ocr")
        return profile

    @property
    def allowed_api_keys(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}

    @property
    def tenant_allowlist(self) -> set[str] | None:
        """None = unrestricted (any non-empty tenant); set = only these tenants."""
        tenants = {t.strip() for t in self.allowed_tenant_ids.split(",") if t.strip()}
        return tenants if tenants else None

    @property
    def api_key_tenant_bindings(self) -> dict[str, set[str] | None]:
        """Map API key → allowed tenants.

        - empty config → {}
        - value ``*`` → None (any tenant, still subject to global allowlist)
        - value ``a|b`` → {"a", "b"}
        """
        raw = (self.api_key_tenant_map or "").strip()
        if not raw:
            return {}
        bindings: dict[str, set[str] | None] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            key, _, tenants_raw = entry.partition(":")
            key = key.strip()
            tenants_raw = tenants_raw.strip()
            if not key:
                continue
            if tenants_raw == "*":
                bindings[key] = None
            else:
                tenants = {t.strip() for t in tenants_raw.split("|") if t.strip()}
                if tenants:
                    bindings[key] = tenants
        return bindings

    @property
    def from_url_host_allowlist(self) -> set[str] | None:
        hosts = {h.strip().lower() for h in self.from_url_allowed_hosts.split(",") if h.strip()}
        return hosts if hosts else None

    @property
    def is_lite(self) -> bool:
        return self.deploy_mode == "lite"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init > env > .env > yaml > secrets
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
