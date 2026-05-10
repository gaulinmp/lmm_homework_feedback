from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response

from app.auth import User, require_role

router = APIRouter(prefix="/admin")


@router.get("/audit/{path:path}")
def audit_placeholder(
    path: str, _user: User = Depends(require_role("admin"))
) -> Response:
    return HTMLResponse(
        f"<p>admin audit placeholder — path={path}</p>", status_code=200
    )
