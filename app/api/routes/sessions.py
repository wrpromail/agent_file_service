from fastapi import APIRouter

from app.api.deps import DbSession, TenantId
from app.schemas.files import SessionFilesResponse
from app.services.lifecycle_service import list_session_files

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get("/{session_id}/files", response_model=SessionFilesResponse)
def get_session_files(
    session_id: str,
    db: DbSession,
    tenant_id: TenantId,
    limit: int = 20,
    offset: int = 0,
) -> SessionFilesResponse:
    """List files in a session with pagination.

    B4 fix: added limit/offset to prevent OOM when sessions contain many files.
    """
    return list_session_files(db, tenant_id=tenant_id, session_id=session_id, limit=limit, offset=offset)