from __future__ import annotations
import logging
import re

import httpx

from .zerobounce import validate_email

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
_EMAIL_SKIP = re.compile(
    r'^(noreply|no-reply|donotreply|bounce|postmaster|mailer-daemon)@'
    r'|\.(png|jpg|gif|svg|webp|ico|css|js|woff|ttf|eot)$',
    re.IGNORECASE,
)


def _extract_email(html: str) -> str:
    """Return the first plausible contact email found in the page HTML."""
    for m in _EMAIL_RE.finditer(html):
        addr = m.group(0)
        if not _EMAIL_SKIP.search(addr):
            return addr.lower()
    return ""


# Signals that indicate the business already has automation/booking/chat in place.
_SIGNALS = [
    # Live chat widgets
    r"intercom",
    r"tawk\.to",
    r"drift\.com",
    r"crisp\.chat",
    r"livechat",
    r"zendesk",
    r"freshchat",
    r"tidio",
    # Online booking
    r"calendly",
    r"acuityscheduling",
    r"booksy",
    r"simplybook",
    r"mindbody",
    r"housecallpro",
    r"servicem8",
    r"jobber",
    r"tradify",
    # Contact / lead forms (structural HTML signals)
    r'<form[^>]{0,200}action[^>]{0,100}contact',
    r'type=["\']submit["\']',
    r'input[^>]{0,100}type=["\']?email',
    r"contact.{0,20}form",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _SIGNALS]


def check_website(url: str, zerobounce_key: str = "") -> tuple[str, str, str]:
    """Fetch the business homepage and return (automation_status, email, email_validation_status).

    automation_status : 'yes' | 'no' | 'unknown'
    email             : first plausible contact email found, or ''
    email_validation_status : ZeroBounce status string, or '' if unavailable
    """
    if not url:
        return "unknown", "", ""

    try:
        resp = httpx.get(
            url,
            timeout=8,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; trade-leads-bot/1.0)"},
        )
        html = resp.text
    except Exception as exc:
        log.debug(f"Website fetch failed for {url}: {exc}")
        return "unknown", "", ""

    automation = "no"
    for pattern in _COMPILED:
        if pattern.search(html):
            automation = "yes"
            break

    email = _extract_email(html)
    email_validation = validate_email(zerobounce_key, email) if email else ""

    return automation, email, email_validation
