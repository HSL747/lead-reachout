from __future__ import annotations
import difflib
import logging
import re

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.company-information.service.gov.uk"
_DIRECTOR_ROLES = {"director", "corporate-director", "nominee-director"}

# suffixes to strip when building a fallback search query
_SUFFIXES = re.compile(
    r"\b(limited|ltd\.?|llp|plc|lp|llc|l\.l\.c\.?)\b\.?$",
    re.IGNORECASE,
)


def _first_name(full_name: str) -> str:
    """Parse first name from Companies House format 'SURNAME, Firstname Middle'."""
    if "," in full_name:
        after_comma = full_name.split(",", 1)[1].strip()
        first = after_comma.split()[0] if after_comma else ""
    else:
        first = full_name.split()[0] if full_name else ""
    return first.capitalize()


def _normalise(name: str) -> str:
    """Produce a normalised version of a company name for search."""
    n = name.strip()
    n = n.replace("&", "and")
    n = re.sub(r"\s+", " ", n)
    return n


def _strip_suffix(name: str) -> str:
    return _SUFFIXES.sub("", name).strip()


def _similarity(a: str, b: str) -> float:
    a_norm = _SUFFIXES.sub("", a.lower().replace("&", "and"))
    b_norm = _SUFFIXES.sub("", b.lower().replace("&", "and"))
    return difflib.SequenceMatcher(None, a_norm.strip(), b_norm.strip()).ratio()


def _best_active_company(items: list[dict], company_name: str) -> str | None:
    """Return the company_number of the active company most similar to company_name."""
    candidates = [i for i in items if i.get("company_status") == "active"]
    if not candidates:
        return None
    scored = sorted(
        candidates,
        key=lambda i: _similarity(i.get("title", ""), company_name),
        reverse=True,
    )
    best = scored[0]
    score = _similarity(best.get("title", ""), company_name)
    log.debug(
        "Best CH match for '%s': '%s' (score %.2f)",
        company_name,
        best.get("title"),
        score,
    )
    # Reject obviously wrong matches
    if score < 0.4:
        return None
    return best["company_number"]


def lookup_director(api_key: str, company_name: str) -> tuple[str, str] | None:
    """Return (first_name, full_name) of the first active director, or None."""
    if not api_key or not company_name:
        return None

    auth = (api_key, "")

    def _search(query: str) -> list[dict]:
        try:
            resp = httpx.get(
                f"{_BASE}/search/companies",
                params={"q": query, "items_per_page": 20},
                auth=auth,
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json().get("items", [])
        except Exception as exc:
            log.warning("CH search failed for '%s': %s", query, exc)
            return []

    # Try the normalised name first, then fall back to stripping the legal suffix
    items = _search(_normalise(company_name))
    company_number = _best_active_company(items, company_name)

    if not company_number:
        short = _strip_suffix(_normalise(company_name))
        if short != _normalise(company_name):
            items = _search(short)
            company_number = _best_active_company(items, company_name)

    if not company_number:
        log.debug("No active CH company found for '%s'", company_name)
        return None

    try:
        resp = httpx.get(
            f"{_BASE}/company/{company_number}/officers",
            params={"items_per_page": 50},
            auth=auth,
            timeout=8,
        )
        resp.raise_for_status()

        for officer in resp.json().get("items", []):
            if officer.get("resigned_on"):
                continue
            if officer.get("officer_role", "").lower() in _DIRECTOR_ROLES:
                full_name = officer.get("name", "")
                first = _first_name(full_name)
                if first:
                    return first, full_name

        log.debug("No active director found for company %s", company_number)
        return None

    except Exception as exc:
        log.warning("CH officers lookup failed for %s: %s", company_number, exc)
        return None
