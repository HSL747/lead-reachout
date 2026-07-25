from __future__ import annotations
import json
import logging
import re
from urllib.parse import urljoin, urlparse
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import anthropic as _anthropic

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 300

_ABOUT_SLUGS = ["/about", "/about-us", "/about_us", "/our-story", "/who-we-are", "/team"]


def _scrape_about_page(website_url: str) -> str:
    """Try common About-page paths. Return stripped text (<=600 chars) or ''."""
    if not website_url:
        return ""
    base = f"{urlparse(website_url).scheme}://{urlparse(website_url).netloc}"
    for slug in _ABOUT_SLUGS:
        try:
            resp = httpx.get(
                urljoin(base, slug),
                timeout=6,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; trade-leads-bot/1.0)"},
            )
            if resp.status_code == 200 and len(resp.text) > 200:
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:600]
        except Exception:
            continue
    return ""


def _build_prompt(
    business_name: str,
    trade: str,
    template_num: int,
    missed_call_score: int,
    top_excerpts: list[str],
    about_context: str,
    rating: float | None,
    review_count: int,
    template_body: str,
) -> str:
    template_context = (
        f"\nThe hook slots into this email template — write it so it reads fluently:\n"
        f"---\n{template_body.strip()}\n---\n"
        if template_body else ""
    )

    if template_num == 2:
        rating_str = f"{rating} stars" if rating else "unknown rating"
        review_str = f"{review_count} reviews" if review_count else "an unknown number of reviews"
        return f"""You are writing cold email openers for a review-generation service targeting UK trades businesses.

Business: {business_name}
Trade: {trade}
Google rating: {rating_str} ({review_str})
{about_context}
{template_context}
Write exactly 3 hook options for an email about helping them get more Google reviews. Rules:
- Short, conversational, friendly openers, under 30 words each
- No exclamation marks, no em-dashes, no salesy language
- Sound like a real person, not marketing copy
- Reference their actual review count or rating where it adds value
- The hook replaces {{{{hook}}}} in the template — make sure the sentence before and after flow naturally
- Rank from MOST specific (references their actual numbers or something unique to this business) to LEAST specific (generic but still relevant)

Also: if the about-page content mentions a specific person by first name, return that name. Otherwise return null.

Respond with ONLY valid JSON, no other text:
{{"hooks": ["hook 1", "hook 2", "hook 3"], "name_from_reviews": null}}"""

    # Template 1: missed-call angle
    if missed_call_score >= 1 and top_excerpts:
        snippets = "\n".join(f'- "{e[:120]}"' for e in top_excerpts[:3])
        review_context = f"Google reviews mention contact/response problems:\n{snippets}"
    else:
        review_context = f"No specific contact-failure reviews found (score {missed_call_score}/3)."

    return f"""You are writing cold email openers for a missed-call text-back service targeting UK trades businesses.

Business: {business_name}
Trade: {trade}
{review_context}
{about_context}
{template_context}
Write exactly 3 hook options. Rules:
- Short, conversational, friendly openers, under 30 words each
- No exclamation marks, no em-dashes, no salesy language
- Sound like a real person, not marketing copy
- The hook replaces {{{{hook}}}} in the template — make sure the sentence before and after flow naturally
- Rank from MOST specific (uses details unique to this business) to LEAST specific (generic but still relevant)
- Examples: Your website clearly encourages visitors to call you via the contact section. I specifically noticed you offer a 24-hour emergency call-out service. - On your website, I notice you've listed your 2 numbers as a way of contacting you. You clearly encourage your site visitors to give you a call. - I noticed you placed your phone number nicely and clearly in the website header (great job!). You're clearly encouraging visitors to call you.

Also: if any review excerpt mentions a specific person by first name (e.g. "Steve was great", "asked for Dan"), return the most commonly mentioned first name. Otherwise return null.

Respond with ONLY valid JSON, no other text:
{{"hooks": ["hook 1", "hook 2", "hook 3"], "name_from_reviews": "Steve"}}"""


def generate_hooks(
    client: "_anthropic.Anthropic | None",
    business_name: str,
    trade: str,
    missed_call_score: int,
    top_excerpts: list[str],
    website_url: str = "",
    template_num: int = 1,
    rating: float | None = None,
    review_count: int = 0,
    template_body: str = "",
) -> tuple[list[str], str]:
    """Return (three hook options, name from reviews/about page or '').

    Hook style matches the template: template 1 = missed-call angle,
    template 2 = review-generation angle. The full template body is passed
    so Claude can see how the hook fits into the email and keep it fluid.
    """
    about_text = _scrape_about_page(website_url)
    about_context = f"About-page content: {about_text}" if about_text else "No about page found."

    prompt = _build_prompt(
        business_name, trade, template_num, missed_call_score,
        top_excerpts, about_context, rating, review_count, template_body,
    )

    try:
        message = client.messages.create(  # type: ignore[union-attr]
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list) or len(hooks) < 1:
            raise ValueError("hooks missing")
        while len(hooks) < 3:
            hooks.append("")
        name = data.get("name_from_reviews") or ""
        return [h for h in hooks[:3] if h], str(name).strip()
    except Exception as exc:
        log.error(f"Hook generation failed for {business_name}: {exc}")
        return [f"I came across {business_name} and had a quick question."], ""
