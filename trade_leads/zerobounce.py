from __future__ import annotations
import logging

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.zerobounce.net/v2"
_KNOWN_STATUSES = {
    "valid", "invalid", "catch-all", "spamtrap",
    "abuse", "do_not_mail", "unknown",
}


def validate_email(api_key: str, email: str) -> str:
    """Return a ZeroBounce status string, or '' on failure or missing key."""
    if not api_key or not email:
        return ""
    try:
        resp = httpx.get(
            f"{_BASE}/validate",
            params={"api_key": api_key, "email": email, "ip_address": ""},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "").lower()
        return status if status in _KNOWN_STATUSES else "unknown"
    except Exception as exc:
        log.debug(f"ZeroBounce validation failed for {email}: {exc}")
        return ""
