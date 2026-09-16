"""Admin routes for the team to review conversations.

    GET /admin/chats?phone=919800000001&limit=50
    Header: X-Admin-Key: <ADMIN_API_KEY>
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(request: Request, x_admin_key: str | None = Header(default=None)) -> None:
    expected: str = request.app.state.settings.admin_api_key
    # An empty ADMIN_API_KEY disables the admin API instead of leaving it open.
    if not expected or not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="invalid admin key")


@router.get("/chats", dependencies=[Depends(require_admin)])
async def recent_chats(
    request: Request,
    phone: str | None = Query(default=None, description="WhatsApp number, e.g. 919800000001"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    phone = (phone or "").strip().lstrip("+") or None
    items = await request.app.state.store.recent_logs(phone=phone, limit=limit)
    return {"count": len(items), "items": items}
