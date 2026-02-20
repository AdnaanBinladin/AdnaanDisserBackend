from __future__ import annotations

from typing import Any

from flask import Request

from src.services.supabase_service import supabase
from src.utils.jwt import decode_jwt


def _extract_ip(req: Request | None) -> str | None:
    if req is None:
        return None
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return req.remote_addr


def actor_from_request(req: Request | None) -> tuple[str | None, str | None]:
    if req is None:
        return None, None
    auth_header = req.headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return None, None
    token = auth_header.split(" ", 1)[1].strip()
    payload = decode_jwt(token)
    if not payload:
        return None, None
    return payload.get("user_id"), payload.get("role")


def log_audit(
    action: str,
    user_id: str | None = None,
    user_role: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    req: Request | None = None,
) -> None:
    try:
        payload = {
            "user_id": user_id,
            "user_role": user_role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata or {},
            "ip_address": _extract_ip(req),
        }
        supabase.table("audit_logs").insert(payload).execute()
    except Exception:
        # Audit logging must never break primary request flow.
        return
