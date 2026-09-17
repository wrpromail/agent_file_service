"""UC 工作台换票 API：``GET /api/auth/uc/status`` · ``POST /api/auth/uc/exchange``。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.uc.session import UcExchangeError, exchange_uc_token, uc_status_payload

router = APIRouter(tags=["uc-auth"])


class UcExchangeBody(BaseModel):
    token: str = Field(default="")


@router.get("/api/auth/uc/status")
def uc_status() -> dict[str, Any]:
    return uc_status_payload()


@router.post("/api/auth/uc/exchange")
def uc_exchange(body: UcExchangeBody) -> dict[str, Any]:
    try:
        return exchange_uc_token(body.token)
    except UcExchangeError as err:
        raise HTTPException(status_code=err.status_code, detail=str(err)) from err
