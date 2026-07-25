from __future__ import annotations
import csv
from pathlib import Path

from .models import Lead

# Business-data fields from the Lead model
_LEAD_FIELDS = list(Lead.model_fields.keys())

# Pipeline fields stored as SQL columns, appended after the business data
_PIPELINE_FIELDS = [
    "status",
    "draft_generated_at",
    "template_number",
    "selected_hook",
    "sent_at",
    "followup1_due",
    "followup2_due",
    "followup1_sent_at",
    "followup2_sent_at",
    "response_received",
    "outcome",
    "notes",
    "subject",
    "gmail_draft_id",
]

ALL_FIELDS = _LEAD_FIELDS + _PIPELINE_FIELDS


def export_csv(leads: list[Lead], output_path: str) -> Path:
    """Export a list of Lead objects (v1 compat — no pipeline columns)."""
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LEAD_FIELDS)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.model_dump())
    return path


def export_csv_full(merged_leads: list[dict], output_path: str) -> Path:
    """Export merged lead dicts (business data + pipeline columns) to CSV."""
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in merged_leads:
            writer.writerow({field: row.get(field, "") for field in ALL_FIELDS})
    return path
