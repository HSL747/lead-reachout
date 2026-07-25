from __future__ import annotations
import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic as _anthropic

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 150

_FALLBACKS = {
    1: lambda name, trade, area: [
        f"Quick question, {name}",
        f"Missed calls — {name}",
        f"{trade.capitalize()} businesses in {area}",
    ],
    2: lambda name, trade, area: [
        f"Your Google reviews, {name}",
        f"More reviews for {name}",
        f"{trade.capitalize()} reviews in {area}",
    ],
}


def generate_subjects(
    client: "_anthropic.Anthropic",
    business_name: str,
    trade: str,
    area: str,
    hook: str,
    template_num: int = 1,
) -> list[str]:
    """Return 3 subject line options matching the chosen template's angle."""

    if template_num == 2:
        service_description = (
            "a review-generation service that automatically texts customers after each job "
            "asking them to leave a Google review"
        )
    else:
        service_description = (
            "a missed-call text-back service that automatically texts customers back "
            "when their call goes unanswered"
        )

    prompt = (
        f"Generate 3 cold email subject lines for an outreach email to '{business_name}', "
        f"a {trade} business in {area}. "
        f"The opening line of the email is: \"{hook}\". "
        f"The email is about {service_description}. "
        f"Rules: under 50 characters each, natural and conversational, no exclamation marks, "
        f"no spam trigger words (free, urgent, act now), not salesy. "
        f"Vary the angle across the 3 options. "
        f"Output ONLY valid JSON in this exact format, no other text: "
        f'{{\"subjects\": [\"subject 1\", \"subject 2\", \"subject 3\"]}}'
    )

    try:
        message = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        subjects = data.get("subjects", [])
        if isinstance(subjects, list) and len(subjects) >= 1:
            while len(subjects) < 3:
                subjects.append("")
            return subjects[:3]
    except Exception as exc:
        log.error(f"Subject generation failed for {business_name}: {exc}")

    return _FALLBACKS.get(template_num, _FALLBACKS[1])(business_name, trade, area)
