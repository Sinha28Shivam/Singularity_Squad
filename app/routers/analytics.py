"""/api/analytics/* routes: read-only spend analytics for admins and approvers."""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.analytics import service
from app.auth.deps import require_approver_or_admin
from app.portal_schemas import UserPublic

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _range(start: Optional[date], end: Optional[date]) -> tuple[Optional[date], Optional[date]]:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="'from' must be on or before 'to'.")
    return start, end


@router.get("/summary")
def analytics_summary(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                      user: UserPublic = Depends(require_approver_or_admin)) -> dict:
    return service.summary(*_range(start, end))


@router.get("/export.csv")
def analytics_export(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                     user: UserPublic = Depends(require_approver_or_admin)) -> Response:
    body = service.export_csv(*_range(start, end))
    suffix = "-".join(d.isoformat() for d in (start, end) if d) or "all"
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="spend-analytics-{suffix}.csv"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
