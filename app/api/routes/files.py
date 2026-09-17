import hashlib
import json
import uuid
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, Response, UploadFile, status
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import AuthDep, DbSession, StorageDep, TenantId
from app.config import get_settings
from app.core.audit import audit_log
from app.core.datetime_utils import parse_expires_at
from app.core.metrics import inc_metric
from app.core.security import assert_tenant_allowed, get_owned_reference
from app.models.parse_result import ParseResult
from app.schemas.content import StructuredContent, build_not_ready_content, mark_content_ready
from app.schemas.files import (
    ArchiveMembersResponse,
    BatchContentItem,
    BatchContentRequest,
    BatchContentResponse,
    BatchStatusItem,
    BatchStatusRequest,
    BatchStatusResponse,
    CloneRequest,
    CloneResponse,
    CustomParseJobListResponse,
    CustomParseJobResponse,
    CustomParseRequest,
    FileDetailResponse,
    FileListResponse,
    FromUrlRequest,
    ParseRequest,
    ParseTriggerResponse,
    ReferenceUpdateRequest,
    UploadResponse,
)
from app.services.custom_parse_service import (
    create_custom_parse_job,
    get_custom_parse_job,
    list_custom_parse_jobs,
)
from app.services.file_service import (
    clone_reference,
    download_reference_stream,
    get_file_detail,
    get_file_status,
    list_archive_members,
    list_files,
    parse_custom_metadata,
    update_reference,
    upload_file,
)
from app.services.lifecycle_service import delete_reference
from app.services.parse_service import (
    SUPPORTED_PARSE_PROFILES,
    filter_structured_content,
    find_cached_parse,
    load_structured_content,
    resolve_parser_for_profile,
    schedule_profile_parse,
)
from app.services.quota_service import QuotaExceededError, check_parse_quota, raise_http_quota
from app.services.url_import_service import import_from_url

router = APIRouter(prefix="/v1/files", tags=["files"])


def _etag_for(*parts: object) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'


def _maybe_not_modified(if_none_match: str | None, etag: str) -> Response | None:
    if not if_none_match:
        return None
    # Allow weak/list forms simply by substring / exact
    candidates = [c.strip() for c in if_none_match.split(",")]
    if etag in candidates or etag.strip('"') in {c.strip('W/"').strip('"') for c in candidates}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return None


def _load_content_for_reference(
    db,
    storage,
    reference,
    *,
    profile: str,
    pages: str | None = None,
    types: str | None = None,
    include_confidence: bool = True,
    smart_mode: bool = True,
    mode: str = "full",
    max_chars: int | None = None,
    offset: int = 0,
    sheets: str | None = None,
) -> tuple[StructuredContent, str, str | None]:
    """Returns (content, etag_source_status, completed_at_iso)."""
    parser, _ = resolve_parser_for_profile(
        db,
        content_hash=reference.content_hash,
        filename=reference.original_filename,
        mime_type=reference.blob.mime_type,
        file_data=b"",
        profile=profile,
    )
    cached = find_cached_parse(
        db,
        reference.content_hash,
        profile=profile,
        parser_name=parser.name,
        parser_version=parser.version,
    )
    if cached is None:
        row = db.scalar(
            select(ParseResult).where(
                ParseResult.content_hash == reference.content_hash,
                ParseResult.parser_name == parser.name,
                ParseResult.parser_version == parser.version,
                ParseResult.profile == profile,
            )
        )
        parse_status = row.status if row else "not_started"
        content = build_not_ready_content(
            content_hash=reference.content_hash,
            profile=profile,
            parser_name=parser.name,
            parser_version=parser.version,
            status=parse_status,
            error=row.error if row else None,
            filename=reference.original_filename,
            mime_type=reference.blob.mime_type,
        )
        completed = row.completed_at.isoformat() if row and row.completed_at else None
        return content, parse_status, completed

    content = load_structured_content(storage, cached)
    filtered = filter_structured_content(
        content,
        pages=pages,
        types=types,
        include_confidence=include_confidence,
        smart_mode=smart_mode,
        mime_type=reference.blob.mime_type,
        filename=reference.original_filename,
        mode=mode,
        max_chars=max_chars,
        offset=offset,
        sheets=sheets,
    )
    ready = mark_content_ready(filtered, status="completed")
    completed = cached.completed_at.isoformat() if cached.completed_at else None
    return ready, "completed", completed


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def create_file(
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    auth: AuthDep,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    agent_id: str | None = Form(default=None),
    uploaded_by: str | None = Form(default=None),
    auto_parse: bool = Form(default=True),
    metadata: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    archive_password: str | None = Form(
        default=None,
        description="可选；zip 解压密码（ZipCrypto）。不落库、不写日志。",
    ),
) -> UploadResponse:
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filename is required")
    try:
        custom_metadata = parse_custom_metadata(metadata)
        parsed_expires_at = parse_expires_at(expires_at)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    password = (archive_password or "").strip() or None

    result = upload_file(
        db,
        storage,
        tenant_id=tenant_id,
        file_obj=file.file,
        filename=file.filename,
        content_type=file.content_type,
        session_id=session_id,
        agent_id=agent_id,
        uploaded_by=uploaded_by,
        custom_metadata=custom_metadata,
        auto_parse=auto_parse,
        expires_at=parsed_expires_at,
        background_tasks=background_tasks,
        archive_password=password,
    )
    db.commit()
    if result.archive is not None:
        inc_metric("archive_expand_total", status=result.archive.status)
    audit_log(
        "upload",
        tenant_id=tenant_id,
        key_id=auth.key_id,
        reference_id=str(result.reference_id),
        deduplicated=result.deduplicated,
    )
    return result


@router.post("/from-url", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def create_file_from_url(
    body: FromUrlRequest,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    auth: AuthDep,
    background_tasks: BackgroundTasks,
) -> UploadResponse:
    try:
        result = import_from_url(
            db,
            storage,
            tenant_id=tenant_id,
            body=body,
            background_tasks=background_tasks,
        )
        db.commit()
    except HTTPException as exc:
        # 4xx = client/config/SSRF；5xx = 远端拉取等服务端失败（勿与 ok 混在同一 error 桶）
        if 400 <= int(exc.status_code) < 500:
            inc_metric("from_url_total", result="client_error")
        else:
            inc_metric("from_url_total", result="error")
        raise
    except Exception:
        inc_metric("from_url_total", result="error")
        raise
    # from_url ⊂ uploads：成功路径仍会经 upload_file 增加 uploads_total
    inc_metric("from_url_total", result="ok")
    if result.archive is not None:
        inc_metric("archive_expand_total", status=result.archive.status)
    audit_log(
        "upload_from_url",
        tenant_id=tenant_id,
        key_id=auth.key_id,
        reference_id=str(result.reference_id),
    )
    return result


@router.get("", response_model=FileListResponse)
def list_files_endpoint(
    db: DbSession,
    tenant_id: TenantId,
    session_id: str | None = None,
    agent_id: str | None = None,
    uploaded_by: str | None = None,
    filename: str | None = None,
    content_hash: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> FileListResponse:
    """List files for the current tenant with optional filters and pagination."""
    return list_files(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        agent_id=agent_id,
        uploaded_by=uploaded_by,
        filename=filename,
        content_hash=content_hash,
        limit=limit,
        offset=offset,
    )


@router.post("/batch-status", response_model=BatchStatusResponse)
def batch_status(
    body: BatchStatusRequest,
    db: DbSession,
    tenant_id: TenantId,
) -> BatchStatusResponse:
    settings = get_settings()
    if len(body.reference_ids) > settings.batch_max_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many reference_ids (max {settings.batch_max_ids})",
        )
    items: list[BatchStatusItem] = []
    for ref_id in body.reference_ids:
        try:
            reference = get_owned_reference(db, tenant_id, ref_id)
            st = get_file_status(db, reference, body.profile)
            items.append(BatchStatusItem(reference_id=ref_id, ok=True, status=st))
        except HTTPException:
            items.append(
                BatchStatusItem(
                    reference_id=ref_id,
                    ok=False,
                    error="File not found",
                    error_code="not_found",
                )
            )
    return BatchStatusResponse(total=len(items), items=items)


@router.post("/batch-content", response_model=BatchContentResponse)
def batch_content(
    body: BatchContentRequest,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
) -> BatchContentResponse:
    settings = get_settings()
    if len(body.reference_ids) > settings.batch_max_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many reference_ids (max {settings.batch_max_ids})",
        )
    if body.profile not in SUPPORTED_PARSE_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"Unsupported profile: {body.profile}", "message_code": "parse_unsupported_profile"},
        )
    items: list[BatchContentItem] = []
    for ref_id in body.reference_ids:
        try:
            reference = get_owned_reference(db, tenant_id, ref_id)
            content, _, _ = _load_content_for_reference(
                db,
                storage,
                reference,
                profile=body.profile,
                pages=body.pages,
                types=body.types,
                include_confidence=body.include_confidence,
                smart_mode=body.smart_mode,
                mode=body.mode,
                max_chars=body.max_chars,
                offset=body.offset,
                sheets=body.sheets,
            )
            items.append(
                BatchContentItem(
                    reference_id=ref_id,
                    ok=True,
                    content=content.model_dump(mode="json"),
                )
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                items.append(
                    BatchContentItem(
                        reference_id=ref_id,
                        ok=False,
                        error="File not found",
                        error_code="not_found",
                    )
                )
            else:
                items.append(
                    BatchContentItem(
                        reference_id=ref_id,
                        ok=False,
                        error=str(exc.detail),
                        error_code="error",
                    )
                )
        except ValueError as exc:
            items.append(
                BatchContentItem(
                    reference_id=ref_id,
                    ok=False,
                    error=str(exc),
                    error_code="parse_unsupported_profile",
                )
            )
    return BatchContentResponse(total=len(items), items=items)


@router.delete("/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_file(
    reference_id: uuid.UUID,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    auth: AuthDep,
) -> None:
    delete_reference(db, storage, tenant_id=tenant_id, reference_id=reference_id)
    db.commit()
    inc_metric("deletes_total")
    audit_log("delete", tenant_id=tenant_id, key_id=auth.key_id, reference_id=str(reference_id))


@router.get("/{reference_id}", response_model=FileDetailResponse)
def get_file(
    reference_id: uuid.UUID,
    db: DbSession,
    tenant_id: TenantId,
) -> FileDetailResponse:
    reference = get_owned_reference(db, tenant_id, reference_id)
    return get_file_detail(db, reference)


@router.patch("/{reference_id}", response_model=FileDetailResponse)
def patch_file(
    reference_id: uuid.UUID,
    body: ReferenceUpdateRequest,
    db: DbSession,
    tenant_id: TenantId,
    auth: AuthDep,
) -> FileDetailResponse:
    reference = get_owned_reference(db, tenant_id, reference_id)
    mode = (body.metadata_mode or "merge").strip().lower()
    if mode not in {"merge", "replace"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="metadata_mode must be merge or replace")
    result = update_reference(db, reference, body, fields_set=set(body.model_fields_set))
    db.commit()
    audit_log("patch", tenant_id=tenant_id, key_id=auth.key_id, reference_id=str(reference_id))
    return result


@router.post(
    "/{reference_id}/clone",
    response_model=CloneResponse,
    status_code=status.HTTP_201_CREATED,
)
def clone_file(
    reference_id: uuid.UUID,
    body: CloneRequest,
    db: DbSession,
    tenant_id: TenantId,
    auth: AuthDep,
) -> CloneResponse:
    """Clone a FileReference into a target tenant, reusing the same Blob."""
    assert_tenant_allowed(body.target_tenant_id, get_settings())
    source_reference = get_owned_reference(db, tenant_id, reference_id)
    result = clone_reference(
        db,
        source_reference=source_reference,
        body=body,
        source_tenant_id=tenant_id,
    )
    db.commit()
    audit_log(
        "clone",
        tenant_id=tenant_id,
        key_id=auth.key_id,
        reference_id=str(result.reference_id),
        source_reference_id=str(reference_id),
        target_tenant_id=body.target_tenant_id,
    )
    return result


@router.get("/{reference_id}/status")
def get_file_parse_status(
    reference_id: uuid.UUID,
    db: DbSession,
    tenant_id: TenantId,
    profile: str | None = None,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    reference = get_owned_reference(db, tenant_id, reference_id)
    payload = get_file_status(db, reference, profile)
    # Fingerprint parse statuses
    status_fp = ",".join(f"{p.profile}:{p.status}" for p in payload.parse_results)
    etag = _etag_for(reference.content_hash, profile or "*", status_fp)
    not_mod = _maybe_not_modified(if_none_match, etag)
    if not_mod is not None:
        return not_mod
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        headers={"ETag": etag},
    )


@router.post("/{reference_id}/parse", response_model=ParseTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_parse(
    reference_id: uuid.UUID,
    body: ParseRequest,
    db: DbSession,
    tenant_id: TenantId,
    auth: AuthDep,
    background_tasks: BackgroundTasks,
) -> ParseTriggerResponse:
    if body.profile not in SUPPORTED_PARSE_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"Unsupported profile: {body.profile}", "message_code": "parse_unsupported_profile"},
        )
    reference = get_owned_reference(db, tenant_id, reference_id)
    try:
        check_parse_quota(db, tenant_id=tenant_id)
    except QuotaExceededError as exc:
        raise_http_quota(exc)
    try:
        row = schedule_profile_parse(
            db,
            reference.content_hash,
            filename=reference.original_filename,
            mime_type=reference.blob.mime_type,
            file_data=b"",
            profile=body.profile,
            force=body.force,
            background_tasks=background_tasks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    audit_log(
        "parse",
        tenant_id=tenant_id,
        key_id=auth.key_id,
        reference_id=str(reference_id),
        profile=body.profile,
        force=body.force,
    )
    return ParseTriggerResponse(profile=body.profile, status=row.status, parse_result_id=row.id)


@router.post(
    "/{reference_id}/custom-parse",
    response_model=CustomParseJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_custom_parse(
    reference_id: uuid.UUID,
    body: CustomParseRequest,
    db: DbSession,
    tenant_id: TenantId,
    auth: AuthDep,
    background_tasks: BackgroundTasks,
) -> CustomParseJobResponse:
    """图片 / 音频：用户自定义 prompt 解析（独立 job 表，不写共享 parse_results）。"""
    reference = get_owned_reference(db, tenant_id, reference_id)
    try:
        check_parse_quota(db, tenant_id=tenant_id)
    except QuotaExceededError as exc:
        raise_http_quota(exc)
    job = create_custom_parse_job(
        db,
        reference,
        body,
        background_tasks=background_tasks,
    )
    db.commit()
    db.refresh(job)
    inc_metric("custom_parse_started_total", media_kind=job.media_kind or "unknown")
    audit_log(
        "custom_parse",
        tenant_id=tenant_id,
        key_id=auth.key_id,
        reference_id=str(reference_id),
        job_id=str(job.id),
    )
    return get_custom_parse_job(
        db,
        tenant_id=tenant_id,
        reference_id=reference_id,
        job_id=job.id,
    )


@router.get(
    "/{reference_id}/custom-parse",
    response_model=CustomParseJobListResponse,
)
def list_custom_parses(
    reference_id: uuid.UUID,
    db: DbSession,
    tenant_id: TenantId,
    limit: int = 20,
    offset: int = 0,
) -> CustomParseJobListResponse:
    get_owned_reference(db, tenant_id, reference_id)
    return list_custom_parse_jobs(
        db,
        tenant_id=tenant_id,
        reference_id=reference_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{reference_id}/custom-parse/{job_id}",
    response_model=CustomParseJobResponse,
)
def get_custom_parse(
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbSession,
    tenant_id: TenantId,
) -> CustomParseJobResponse:
    get_owned_reference(db, tenant_id, reference_id)
    return get_custom_parse_job(
        db,
        tenant_id=tenant_id,
        reference_id=reference_id,
        job_id=job_id,
    )


@router.get("/{reference_id}/archive/members", response_model=ArchiveMembersResponse)
def get_archive_members(
    reference_id: uuid.UUID,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    archive_password: str | None = None,
) -> ArchiveMembersResponse:
    reference = get_owned_reference(db, tenant_id, reference_id)
    password = (archive_password or "").strip() or None
    return list_archive_members(db, storage, reference, password=password)


@router.get("/{reference_id}/download")
def download_file(
    reference_id: uuid.UUID,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    auth: AuthDep,
) -> StreamingResponse:
    reference = get_owned_reference(db, tenant_id, reference_id)
    filename = reference.original_filename
    mime_type = reference.blob.mime_type
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    encoded_name = quote(filename)
    disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    inc_metric("downloads_total")
    audit_log("download", tenant_id=tenant_id, key_id=auth.key_id, reference_id=str(reference_id))
    return StreamingResponse(
        download_reference_stream(storage, reference),
        media_type=mime_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/{reference_id}/content")
def get_file_content(
    reference_id: uuid.UUID,
    db: DbSession,
    storage: StorageDep,
    tenant_id: TenantId,
    profile: str = "layout",
    pages: str | None = None,
    types: str | None = None,
    include_confidence: bool = True,
    smart_mode: bool = True,
    mode: str = "full",
    max_chars: int | None = None,
    offset: int = 0,
    sheets: str | None = None,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    """返回解析后的结构化内容（方案 B：未就绪也 200）。

    - ready=true：正文可用（blocks / metadata.markdown）
    - ready=false：解析未开始/排队中/进行中/失败；带 message / message_code 文案
    - smart_mode（默认 true）：表格丢弃无实质内容空 sheet；false 取原始 Markdown
    - mode=full|outline；max_chars/offset 字符窗口；sheets 过滤电子表格 sheet
    - ETag / If-None-Match → 304
    """
    if profile not in SUPPORTED_PARSE_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"Unsupported profile: {profile}", "message_code": "parse_unsupported_profile"},
        )
    reference = get_owned_reference(db, tenant_id, reference_id)
    try:
        content, parse_status, completed_at = _load_content_for_reference(
            db,
            storage,
            reference,
            profile=profile,
            pages=pages,
            types=types,
            include_confidence=include_confidence,
            smart_mode=smart_mode,
            mode=mode,
            max_chars=max_chars,
            offset=offset,
            sheets=sheets,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(exc), "message_code": "parse_unsupported_profile"},
        ) from exc

    view_fp = f"{smart_mode}:{mode}:{max_chars}:{offset}:{sheets}:{pages}:{types}:{include_confidence}"
    etag = _etag_for(reference.content_hash, profile, parse_status, completed_at, view_fp)
    not_mod = _maybe_not_modified(if_none_match, etag)
    if not_mod is not None:
        return not_mod
    ready_flag = "true" if content.ready else "false"
    inc_metric("content_reads_total", profile=profile, ready=ready_flag)
    return JSONResponse(
        content=content.model_dump(mode="json"),
        headers={"ETag": etag},
    )
