"""Public sandbox endpoints + an audited real-write attempt route.

Anonymous visitors only ever touch the session-scoped sandbox tables. The real write
goes through Lambda ERP's own registered-action gate (manager+); a visitor attempting it
is refused by that gate, and the refusal is recorded in the audit log.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api import services
from api.auth import get_current_user

from . import service

router = APIRouter(prefix="/inventory-ops", tags=["inventory-ops"])
_SID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_HITS: dict[str, deque] = defaultdict(deque)


def _limit(request: Request, per_minute: int = 40) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _HITS[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(status_code=429, detail="Too many requests; slow down.")
    q.append(now)


def _sid(value: str) -> str:
    if not _SID.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="Invalid session id.")
    return value


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except service.ReorderError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


class SandboxPropose(BaseModel):
    session_id: str


class SandboxConfirm(BaseModel):
    session_id: str
    proposal_id: str = Field(max_length=40)
    confirm: bool = False


class RealConfirm(BaseModel):
    proposal_id: str = Field(max_length=40)
    confirm: bool = False
    session_id: str | None = None  # optional: lets a denied attempt show in the visitor's own audit log


@router.get("/state")
def state(request: Request, session_id: str):
    _limit(request)
    sid = _sid(session_id)
    return {"mode": "sandbox", "stock": service.low_stock(sid), "audit": service.audit_events(session_id=sid, limit=20), "caps": service._caps()}


@router.post("/sandbox/propose")
def sandbox_propose(body: SandboxPropose, request: Request):
    _limit(request)
    return _run(service.propose, user=None, session_id=_sid(body.session_id), scope="sandbox")


@router.post("/sandbox/confirm")
def sandbox_confirm(body: SandboxConfirm, request: Request):
    _limit(request)
    return _run(service.confirm_sandbox, session_id=_sid(body.session_id), proposal_id=body.proposal_id, confirm=body.confirm)


@router.post("/real/confirm")
def real_confirm(body: RealConfirm, request: Request):
    """Attempt the real (shared-ERP) write. Only manager+/admin succeed."""
    _limit(request)
    try:
        user = get_current_user(request)
    except HTTPException:
        user = None
    if user is None:
        service.audit("denied_unauthenticated", role="anonymous", proposal_id=body.proposal_id,
                      session_id=_sid(body.session_id) if body.session_id else None)
        raise HTTPException(status_code=401, detail="Sign in as a manager to confirm real purchase orders.")
    try:
        return _run(services.run_registered_action, "confirm_reorder", body.model_dump(exclude={"session_id"}), user)
    except HTTPException:
        raise
    except PermissionError as exc:
        service.audit("denied_role", actor=user.get("email"), role=user.get("role"), proposal_id=body.proposal_id)
        raise HTTPException(status_code=403, detail=str(exc))
