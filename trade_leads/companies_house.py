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

_MALE_TITLES = {"mr", "sir", "lord", "rev", "revd", "dr"}

# Common UK male first names (lowercase) for fallback gender inference
_MALE_NAMES = {
    "james", "john", "robert", "michael", "william", "david", "richard",
    "joseph", "thomas", "charles", "christopher", "daniel", "matthew",
    "anthony", "donald", "mark", "paul", "steven", "steve", "andrew",
    "kenneth", "george", "kevin", "brian", "edward", "ronald", "timothy",
    "jason", "jeffrey", "ryan", "gary", "jacob", "nicholas", "eric",
    "jonathan", "stephen", "larry", "scott", "frank", "justin", "brandon",
    "raymond", "gregory", "samuel", "benjamin", "patrick", "jack", "dennis",
    "peter", "harry", "adam", "ian", "alan", "barry", "wayne", "graham",
    "neil", "craig", "lee", "dean", "shaun", "darren", "simon", "martin",
    "colin", "gareth", "phil", "phillip", "derek", "stewart", "stuart",
    "brett", "robin", "ben", "mike", "dave", "chris", "tony", "rob",
    "matt", "dan", "luke", "sean", "liam", "owen", "callum", "jamie",
    "nathan", "carl", "karl", "keith", "glen", "glen", "clive", "nigel",
    "geoff", "geoffrey", "julian", "alex", "tim", "sam", "joe", "jon",
}


def _first_name(full_name: str) -> str:
    """Parse first name from Companies House format 'SURNAME, Firstname Middle'."""
    if "," in full_name:
        after_comma = full_name.split(",", 1)[1].strip()
        first = after_comma.split()[0] if after_comma else ""
    else:
        first = full_name.split()[0] if full_name else ""
    return first.capitalize()


def _is_likely_male(officer: dict) -> bool:
    """Return True if the officer appears to be male, using title then first-name heuristic."""
    title = officer.get("title", "").lower().rstrip(".")
    if title in _MALE_TITLES:
        return True
    first = _first_name(officer.get("name", "")).lower()
    return first in _MALE_NAMES


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

        active_directors = [
            o for o in resp.json().get("items", [])
            if not o.get("resigned_on")
            and o.get("officer_role", "").lower() in _DIRECTOR_ROLES
        ]

        if not active_directors:
            log.debug("No active director found for company %s", company_number)
            return None

        # Prefer male director (handles husband-and-wife pairs)
        male = next((o for o in active_directors if _is_likely_male(o)), None)
        chosen = male or active_directors[0]

        full_name = chosen.get("name", "")
        first = _first_name(full_name)
        return (first, full_name) if first else None

    except Exception as exc:
        log.warning("CH officers lookup failed for %s: %s", company_number, exc)
        return None
