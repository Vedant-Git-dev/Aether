"""REST endpoints. `settings` and component instances are read from
`request.app.state`, which the lifespan in main.py populates before the
server accepts traffic.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

log = logging.getLogger("aether.api")

router = APIRouter()

# a full-screen PNG is a few MB; anything past this is not a screenshot
MAX_CAPTURE_BYTES = 20 * 1024 * 1024


def _authorized(request: Request, token: str | None) -> bool:
    candidate = token or request.headers.get("x-aether-token", "")
    return bool(candidate) and hmac.compare_digest(candidate, request.app.state.settings.api_token)


@router.post("/api/screen-capture", status_code=202)
async def screen_capture(
    request: Request,
    token: str | None = None,
    file: UploadFile = File(...),
    note: str = Form(""),
) -> dict:
    """The companion's upload point. Perception-only: the screenshot becomes
    a described, encrypted memory event — no action tools come from it."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    screen_vision = getattr(request.app.state, "screen_vision", None)
    if screen_vision is None:
        raise HTTPException(
            status_code=503,
            detail="screen vision is not configured (no vision-capable provider)",
        )
    png = await file.read(MAX_CAPTURE_BYTES + 1)
    if len(png) > MAX_CAPTURE_BYTES:
        raise HTTPException(status_code=413, detail="screenshot too large")
    if not png:
        raise HTTPException(status_code=400, detail="empty upload")
    result = await screen_vision.record(png, note)
    if result is None:  # defensive — record() only returns None pre-check
        raise HTTPException(status_code=503, detail="no vision provider")
    return {"stored": result.stored, "reason": result.reason, "event_id": result.event_id}
