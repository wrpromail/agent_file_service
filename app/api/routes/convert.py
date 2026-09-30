"""file_convert 兼容 REST：路径与 JSON 对齐，须 Bearer + X-Tenant-Id。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

from app.api.deps import AuthDep, DbSession, StorageDep
from app.config import get_settings
from app.core.audit import audit_log
from app.core.metrics import inc_metric
from app.services.convert_service import dispatch_or_local, raise_as_http

router = APIRouter(tags=["convert-compat"])


def _base_url(request: Request) -> str:
    configured = (get_settings().convert_compat_public_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _run(op: str, db, storage, auth: AuthDep, request: Request, **kwargs) -> dict[str, Any]:
    kwargs["tenant_id"] = auth.tenant_id
    kwargs["public_base_url"] = _base_url(request)
    try:
        result = dispatch_or_local(op, db, storage, **kwargs)
        db.commit()
        inc_metric("convert_compat_total", op=op, result="ok")
        audit_log(
            f"convert_{op}",
            tenant_id=auth.tenant_id,
            key_id=auth.key_id,
            reference_id=result.get("reference_id") or result.get("task_id"),
        )
        return result
    except Exception as exc:
        db.rollback()
        inc_metric("convert_compat_total", op=op, result="error")
        raise_as_http(exc)
        raise  # pragma: no cover


class MarkdownPayload(BaseModel):
    markdown_content: str = Field(..., min_length=1)


class ConversionResponse(BaseModel):
    message: str
    pdf_url: str | None = None
    pdf_base64: str | None = None
    file_path: str | None = None
    task_id: str
    reference_id: str | None = None


class OfficeFilePayload(BaseModel):
    file_url: str = Field(..., description="Office 文件的 URL")
    file_format: str = Field(..., pattern=r"^(doc|docx|ppt|pptx|xls|xlsx)$")


class OfficeConversionResponse(BaseModel):
    message: str
    markdown_content: str
    task_id: str
    file_format: str
    processing_time: float
    cache_hit: bool = False
    reference_id: str | None = None


class SheetConfig(BaseModel):
    sheet_name: str
    data: list[dict[str, Any]]
    mapping: dict[str, str] | None = None


class ExcelFileConfig(BaseModel):
    filename: str
    sheets: list[SheetConfig]


class LegacyColumnConfig(BaseModel):
    key: str
    name: str


class LegacySheetData(BaseModel):
    columns: list[LegacyColumnConfig]
    rows: list[dict[str, Any]] = Field(default_factory=list)


class LegacySheetConfig(BaseModel):
    sheet_name: str
    data: LegacySheetData


class ExcelGenerationPayload(BaseModel):
    files: list[ExcelFileConfig] | None = None
    source: list[LegacySheetConfig] | None = None
    file_name: str | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if not self.files and not self.source:
            raise ValueError("files 与 source 至少需要提供一种")
        return self


class ExcelGenerationResponse(BaseModel):
    message: str
    task_id: str
    file_urls: dict[str, str]
    file_ids: dict[str, str] | None = None
    reference_id: str | None = None
    file_base64: str | None = None


class DocumentToImagePayload(BaseModel):
    file_url: str
    file_format: Literal["doc", "docx", "pdf"]
    image_format: Literal["jpeg", "png"] = "jpeg"
    quality: int = Field(default=80, ge=30, le=95)
    max_width: Optional[int] = Field(default=1800, ge=600, le=4096)
    dpi: int = Field(default=220, ge=72, le=500)
    delivery_mode: Literal["storage", "base64"] = "storage"
    pack_mode: Literal["zip", "list"] = "zip"


class PageImageResult(BaseModel):
    page: int
    width: int
    height: int
    size: int
    filename: str
    download_url: str | None = None
    base64_data: str | None = None


class ImageConversionResponse(BaseModel):
    message: str
    task_id: str
    delivery_mode: Literal["storage", "base64"]
    pack_mode: Literal["zip", "list"]
    page_count: int
    archive_filename: str | None = None
    archive_url: str | None = None
    archive_base64: str | None = None
    images: list[PageImageResult] | None = None
    reference_id: str | None = None


class ExtractTextPayload(BaseModel):
    file_url: str
    file_format: str = Field(..., pattern=r"^(doc|docx|ppt|pptx|xls|xlsx|pdf)$")


class ExtractTextResponse(BaseModel):
    message: str
    text_content: str
    task_id: str
    file_format: str
    processing_time: float
    cache_hit: bool = False
    reference_id: str | None = None


@router.post("/convert", response_model=ConversionResponse)
def convert_markdown(
    payload: MarkdownPayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    return _run(
        "markdown_to_pdf",
        db,
        storage,
        auth,
        request,
        markdown_content=payload.markdown_content,
    )


@router.post("/convert-office", response_model=OfficeConversionResponse)
def convert_office(
    payload: OfficeFilePayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    return _run(
        "office_to_markdown",
        db,
        storage,
        auth,
        request,
        file_url=payload.file_url,
        file_format=payload.file_format,
    )


@router.post("/convert-doc", response_model=OfficeConversionResponse)
def convert_doc(
    payload: OfficeFilePayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    return _run(
        "office_to_markdown",
        db,
        storage,
        auth,
        request,
        file_url=payload.file_url,
        file_format="doc",
    )


@router.post("/extract-text", response_model=ExtractTextResponse)
def extract_text(
    payload: ExtractTextPayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    return _run(
        "extract_text",
        db,
        storage,
        auth,
        request,
        file_url=payload.file_url,
        file_format=payload.file_format,
    )


@router.post("/convert-to-images", response_model=ImageConversionResponse)
def convert_to_images(
    payload: DocumentToImagePayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    return _run(
        "document_to_images",
        db,
        storage,
        auth,
        request,
        file_url=payload.file_url,
        file_format=payload.file_format,
        image_format=payload.image_format,
        quality=payload.quality,
        max_width=payload.max_width,
        dpi=payload.dpi,
        delivery_mode=payload.delivery_mode,
        pack_mode=payload.pack_mode,
    )


@router.post("/generate-excel", response_model=ExcelGenerationResponse)
def generate_excel(
    payload: ExcelGenerationPayload,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    auth: AuthDep,
) -> dict[str, Any]:
    files = None
    if payload.files:
        files = [f.model_dump() for f in payload.files]
    source = None
    if payload.source:
        source = [s.model_dump() for s in payload.source]
    return _run(
        "generate_excel",
        db,
        storage,
        auth,
        request,
        files=files,
        source=source,
        file_name=payload.file_name,
    )


@router.get("/cache/stats")
def get_cache_stats(db: DbSession, auth: AuthDep) -> dict[str, Any]:
    from app.services.convert_service import cache_stats

    return cache_stats(db)


@router.delete("/cache/clear")
def clear_cache(db: DbSession, storage: StorageDep, auth: AuthDep, days: int = 30) -> dict[str, Any]:
    from app.services.convert_service import clear_old_cache

    deleted = clear_old_cache(db, storage, days=days)
    db.commit()
    audit_log("convert_cache_clear", tenant_id=auth.tenant_id, key_id=auth.key_id, days=days)
    return {
        "message": f"Cleared {deleted} cache records",
        "days": days,
        "deleted_count": deleted,
    }
