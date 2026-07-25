from __future__ import annotations
import logging
import os
import re
import time
from datetime import datetime, timezone

import click
from pathlib import Path
from dotenv import load_dotenv

from .companies_house import lookup_director
from .export import export_csv_full
from .gmail import authenticate as gmail_authenticate, build_html_body, create_draft as gmail_create_draft, get_signature_html, is_configured as gmail_is_configured, next_send_slot, send_draft as gmail_send_draft
from . import queue as send_queue
from .hook_writer import generate_hooks
from .models import Lead
from .subject_writer import generate_subjects
from .places import fetch_details, text_search
from .scorer import score_reviews
from .storage import (
    get_all_leads_with_pipeline,
    get_due_followups,
    get_followup_drafts_due,
    get_initial_drafts_due,
    get_lead_by_id,
    get_next_uncontacted,
    get_scheduled_send_times,
    get_status_summary,
    init_db,
    save_lead,
    update_lead_pipeline,
    followup_due_dates,
    _parse_area,
)
from .templates import TemplateManager
from .website_check import check_website

load_dotenv(Path.cwd() / ".env")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_TEMPLATES_DIR = "templates"

_RISKY_STATUSES = {"invalid", "do_not_mail"}


def _make_anthropic_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


_COMPANY_SUFFIX_RE = re.compile(
    r"[\s,]*(ltd\.?|limited|plc|llp)\s*$",
    re.IGNORECASE,
)


def _clean_company_name(company: str) -> str:
    """Strip legal suffixes (Ltd, Limited, PLC, LLP) from a company name."""
    return _COMPANY_SUFFIX_RE.sub("", company).strip()


def _trade_plural(trade: str) -> str:
    t = trade.strip().lower()
    return t if t.endswith("s") else t + "s"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Trade leads prospecting tool for missed-call-text-back outreach."""


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--trade", required=True, help="Trade type, e.g. 'plumber'")
@click.option("--location", required=True, help="Location, e.g. 'Guildford, Surrey'")
@click.option("--radius", default=10, show_default=True, help="Search radius in km")
@click.option("--limit", default=50, show_default=True, help="Max businesses to fetch")
@click.option("--dry-run", is_flag=True, help="Skip ZeroBounce validation (saves API credits)")
def search(trade: str, location: str, radius: int, limit: int, dry_run: bool) -> None:
    """Search for trade businesses and score them as outreach prospects."""
    google_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    zerobounce_key = os.environ.get("ZEROBOUNCE_API_KEY", "") if not dry_run else ""

    if not google_key:
        raise click.ClickException("GOOGLE_PLACES_API_KEY not set in .env")

    init_db()

    click.echo(f"Searching for '{trade}' near '{location}' (radius {radius} km, limit {limit})...")
    place_ids = text_search(google_key, trade, location, radius, limit)
    total = len(place_ids)
    click.echo(f"Found {total} places. Fetching details...\n")

    for i, place_id in enumerate(place_ids, 1):
        details = fetch_details(google_key, place_id)
        if not details:
            click.echo(f"  [{i}/{total}] Skipped (details unavailable)")
            continue

        review_texts = [r.text for r in details.reviews if r.text]
        score, excerpts = score_reviews(review_texts)

        automation, email, email_validation = check_website(details.website, zerobounce_key)

        lead = Lead(
            name=details.name,
            phone=details.phone,
            email=email,
            website=details.website,
            address=details.address,
            rating=details.rating,
            review_count=details.review_count,
            missed_call_score=score,
            top_review_excerpts=" | ".join(excerpts),
            has_existing_automation=automation,
            personalised_hook="",
            google_maps_url=details.google_maps_url,
            trade=trade,
            email_validation_status=email_validation,
        )

        save_lead(place_id, lead.model_dump())

        score_badge = f"score {score}" + (" !" if score >= 2 else "")
        validation_badge = f", email: {email_validation}" if email_validation else ""
        click.echo(f"  [{i}/{total}] {details.name} - {score_badge}, automation: {automation}{validation_badge}")

        time.sleep(0.3)

    click.echo(f"\nDone. Run 'trade-leads draft' to start composing outreach.")


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--id", "lead_id", type=int, default=None, help="Numeric lead ID (from 'list'). Omit to use next uncontacted.")
@click.option("--template", "template_num", type=int, default=None, help="Override template number (1-5)")
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging")
def draft(lead_id: int | None, template_num: int | None, debug: bool) -> None:
    """Write a hook, pick a template, and save the draft email."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    init_db()
    tm = TemplateManager(_TEMPLATES_DIR)

    # Load lead
    if lead_id is not None:
        lead = get_lead_by_id(lead_id)
        if not lead:
            raise click.ClickException(f"No lead found with ID {lead_id}")
    else:
        lead = get_next_uncontacted()
        if not lead:
            raise click.ClickException(
                "No uncontacted leads with valid emails found. "
                "Run 'trade-leads search' or check your lead statuses."
            )

    rowid = lead["_rowid"]
    place_id = lead["_place_id"]
    name = lead.get("name", "")
    display_name = _clean_company_name(name)
    trade = lead.get("trade", "unknown")
    address = lead.get("address", "")
    area = _parse_area(address)
    score = lead.get("missed_call_score", 0)
    email = lead.get("email", "")
    email_status = lead.get("email_validation_status", "")
    website = lead.get("website", "")
    rating = lead.get("rating")
    review_count = lead.get("review_count", 0)
    excerpts = [e for e in lead.get("top_review_excerpts", "").split(" | ") if e]
    notes = lead.get("notes") or ""

    # Display lead summary
    click.echo("\n" + "=" * 60)
    click.echo(f"  Lead ID  : {rowid}")
    click.echo(f"  Business : {name}")
    click.echo(f"  Trade    : {trade}")
    click.echo(f"  Area     : {area or address}")
    click.echo(f"  Email    : {email or '(none)'}")
    click.echo(f"  Website  : {website or '(none)'}")
    click.echo(f"  Rating   : {rating} ({review_count} reviews)  |  Missed-call score: {score}/3")
    if excerpts:
        click.echo(f"  Reviews  :")
        for excerpt in excerpts:
            click.echo(f"    \"{excerpt}\"")
    if notes:
        click.echo(f"  Notes    : {notes}")
    click.echo("=" * 60 + "\n")

    # Email validation warning
    if email_status in _RISKY_STATUSES:
        click.echo(f"  [!] Email validation status: {email_status.upper()}")
        if not click.confirm("  Proceed with this lead anyway?", default=False):
            click.echo("Aborted.")
            return
        click.echo()

    # Template selection (before hook generation so hooks match the template angle)
    click.echo()
    _show_template_list(tm)

    if template_num is None:
        override = click.prompt(
            "Template number",
            default="1",
            show_default=True,
        ).strip()
        try:
            template_num = int(override)
        except ValueError:
            template_num = 1

    # Companies House name lookup
    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    ch_result = lookup_director(ch_key, name) if ch_key else None

    # Hook generation — style matches the chosen template
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    selected_hook = ""
    review_name = ""
    if anthropic_key:
        try:
            client = _make_anthropic_client(anthropic_key)
            hooks, review_name = generate_hooks(
                client,
                business_name=name,
                trade=trade,
                missed_call_score=score,
                top_excerpts=excerpts,
                website_url=website,
                template_num=template_num,
                rating=rating,
                review_count=review_count,
                template_body=tm.get_raw(template_num),
            )
            click.echo("\nHook options:")
            for i, h in enumerate(hooks, 1):
                click.echo(f"  {i}. {h}")
            click.echo()
            raw = click.prompt(
                "Select hook [1/2/3] or type your own",
                default="1",
                show_default=True,
            ).strip()
            if raw in ("1", "2", "3"):
                idx = int(raw) - 1
                selected_hook = hooks[idx] if idx < len(hooks) else hooks[0]
            else:
                selected_hook = raw
        except Exception as exc:
            log.warning(f"Hook generation failed: {exc}")
            selected_hook = click.prompt("Enter your hook for this email").strip()
    else:
        selected_hook = click.prompt("Enter your hook for this email").strip()

    # Determine recipient name
    if ch_result:
        found_name, ch_full = ch_result
        name_source = f"Companies House: {ch_full}"
    elif review_name:
        found_name = review_name
        name_source = "reviews"
    else:
        found_name = ""
        name_source = ""

    if found_name:
        click.echo(f"\nName: {found_name}  ({name_source})")
        raw_name = click.prompt(
            "Press Enter to use this name or type a different one",
            default=found_name,
            show_default=False,
        ).strip()
    else:
        click.echo("\nNo name found automatically.")
        raw_name = click.prompt(
            "Recipient name (Enter to leave blank)",
            default="",
            show_default=False,
        ).strip()
    confirmed_name = raw_name

    # Fill template
    try:
        fill_kwargs: dict[str, str] = dict(
            company=display_name,
            trade=trade,
            trade_plural=_trade_plural(trade),
            area=area or "your area",
            hook=selected_hook,
        )
        if confirmed_name:
            fill_kwargs["name"] = confirmed_name
        draft_text = tm.fill(template_num, **fill_kwargs)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    click.echo("\n" + "-" * 60)
    click.echo("DRAFT EMAIL")
    click.echo("-" * 60)
    click.echo(draft_text)
    click.echo("-" * 60 + "\n")

    # Subject line
    subject = _pick_subject(display_name, trade, area, selected_hook, template_num=template_num)

    # Save to DB
    now = _now_iso()
    update_lead_pipeline(
        place_id,
        draft=draft_text,
        draft_generated_at=now,
        selected_hook=selected_hook,
        template_number=template_num,
        subject=subject,
        status="drafted",
    )
    click.echo(f"Draft saved (lead ID {rowid}).")

    # Queue for scheduled send via Railway worker
    if email and gmail_is_configured():
        try:
            service = gmail_authenticate()
            already_scheduled = get_scheduled_send_times()
            slot = next_send_slot(already_scheduled)
            slot_str = slot.strftime("%a %d %b at %H:%M")

            sig_html  = get_signature_html(service)
            body_html = build_html_body(draft_text, sig_html)

            send_queue.init_queue()
            send_queue.enqueue(email, subject, draft_text, body_html, slot, lead_ref=str(rowid))

            update_lead_pipeline(
                place_id,
                scheduled_send_at=slot.isoformat(),
                status="emailed",
                sent_at=slot.isoformat(),
            )
            fu1, fu2 = followup_due_dates(slot.isoformat())
            update_lead_pipeline(place_id, followup1_due=fu1, followup2_due=fu2)
            click.echo(f"Queued — Railway will send this on {slot_str}.")
        except Exception as exc:
            click.echo(f"Scheduling failed: {exc}")
    elif email and not gmail_is_configured():
        click.echo("(Gmail not connected — see README to set up one-click draft creation)")

    click.echo()
    if click.confirm("Draft next lead?", default=True):
        ctx = click.get_current_context()
        ctx.invoke(draft, lead_id=None, template_num=None)


def _show_template_list(tm: TemplateManager) -> None:
    for num, preview in tm.list_templates():
        click.echo(f"  {num}. {preview}")


def _pick_subject(business_name: str, trade: str, area: str, hook: str, template_num: int = 1) -> str:
    """Generate subject line options with Claude if available, else prompt."""
    if template_num == 2:
        hardcoded = f"More Google reviews for {business_name}"
    else:
        hardcoded = f"Missed calls are losing {business_name} jobs"
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            client = _make_anthropic_client(anthropic_key)
            subjects = generate_subjects(client, business_name, trade, area, hook, template_num=template_num)
            all_subjects = subjects + [hardcoded]
            click.echo("\nSubject line options:")
            for i, s in enumerate(all_subjects, 1):
                click.echo(f"  {i}. {s}")
            click.echo()
            raw = click.prompt(
                "Select subject [1-4] or type a custom one",
                default="1",
                show_default=True,
            ).strip()
            if raw in ("1", "2", "3", "4"):
                return all_subjects[int(raw) - 1]
            return raw
        except Exception as exc:
            log.warning(f"Subject generation failed: {exc}")

    click.echo("\nSubject line options:")
    click.echo(f"  1. {hardcoded}")
    click.echo()
    raw = click.prompt(
        "Select subject [1] or type a custom one",
        default="1",
        show_default=True,
    ).strip()
    if raw == "1":
        return hardcoded
    return raw


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--id", "lead_id", type=int, required=True, help="Numeric lead ID")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing to DB")
def send(lead_id: int, dry_run: bool) -> None:
    """Mark a drafted email as sent and set follow-up due dates."""
    init_db()
    lead = get_lead_by_id(lead_id)
    if not lead:
        raise click.ClickException(f"No lead found with ID {lead_id}")

    if not lead.get("draft"):
        raise click.ClickException(
            f"No draft found for lead ID {lead_id}. Run 'trade-leads draft --id {lead_id}' first."
        )

    name = lead.get("name", "")
    email = lead.get("email", "")

    click.echo(f"\nTo: {email} ({name})")
    click.echo("-" * 60)
    click.echo(lead["draft"])
    click.echo("-" * 60 + "\n")

    if not click.confirm("Mark this email as sent?", default=False):
        click.echo("Aborted.")
        return

    now = _now_iso()
    fu1, fu2 = followup_due_dates(now)

    if not dry_run:
        update_lead_pipeline(
            lead["_place_id"],
            status="emailed",
            sent_at=now,
            followup1_due=fu1,
            followup2_due=fu2,
        )
        click.echo(f"\nMarked as sent.")
        click.echo(f"  Follow-up 1 due : {fu1}")
        click.echo(f"  Follow-up 2 due : {fu2}")
        click.echo(f"\nRun 'trade-leads followup' to see due follow-ups.")
    else:
        click.echo(f"\n[dry-run] Would mark as sent.")
        click.echo(f"  Follow-up 1 due : {fu1}")
        click.echo(f"  Follow-up 2 due : {fu2}")


# ---------------------------------------------------------------------------
# followup
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview scheduled times without sending or writing to DB")
def followup(dry_run: bool) -> None:
    """Auto-schedule all due follow-ups, with the option to exclude responders first."""
    init_db()
    tm = TemplateManager(_TEMPLATES_DIR)

    due = get_due_followups()
    if not due:
        click.echo("No follow-ups due. Check back later or run 'trade-leads status'.")
        return

    # Show what's due
    click.echo(f"\n{len(due)} follow-up(s) due:\n")
    click.echo(f"  {'ID':>4}  {'Business':<32}  {'Email':<30}  Slot")
    click.echo("  " + "-" * 78)
    for lead in due:
        slot_label, due_date = _which_followup_slot(lead)
        click.echo(
            f"  {lead['_rowid']:>4}  {lead.get('name', ''):<32}  "
            f"{lead.get('email', ''):<30}  {slot_label} (due {due_date})"
        )

    # Exclusions — people who already replied
    click.echo()
    raw_exclude = click.prompt(
        "Email addresses to exclude (already responded) — comma-separated, or Enter to skip",
        default="",
        show_default=False,
    ).strip()
    exclude_emails = {e.strip().lower() for e in raw_exclude.split(",") if e.strip()}

    to_process = [l for l in due if l.get("email", "").lower() not in exclude_emails]
    skipped = len(due) - len(to_process)

    if skipped:
        click.echo(f"  Excluding {skipped} lead(s) — marking as responded.")
        if not dry_run:
            for lead in due:
                if lead.get("email", "").lower() in exclude_emails:
                    update_lead_pipeline(lead["_place_id"], status="responded")

    if not to_process:
        click.echo("Nothing left to schedule.")
        return

    # Check templates have content
    if not tm.list_templates():
        raise click.ClickException(
            "No follow-up templates found with content. "
            "Fill in templates/followup_1.txt first."
        )

    if not gmail_is_configured():
        raise click.ClickException(
            "Gmail not configured — cannot schedule sends. "
            "Add credentials.json to use scheduled sending."
        )

    # Build and schedule each follow-up
    click.echo(f"\nScheduling {len(to_process)} follow-up(s)...")
    service = gmail_authenticate()
    sig_html = get_signature_html(service)
    send_queue.init_queue()
    already_scheduled = get_scheduled_send_times()

    results: list[tuple[str, datetime]] = []
    errors: list[str] = []

    for lead in to_process:
        slot_label, _ = _which_followup_slot(lead)
        followup_num = 1 if slot_label == "follow-up 1" else 2

        name       = lead.get("name", "")
        trade      = lead.get("trade", "unknown")
        address    = lead.get("address", "")
        area       = _parse_area(address)
        email      = lead.get("email", "")
        sent_at    = lead.get("sent_at", "")
        sent_date  = sent_at[:10] if sent_at else "unknown date"
        orig_hook  = lead.get("selected_hook", "")
        orig_subj  = lead.get("subject") or f"Following up — {_clean_company_name(name)}"
        subject    = f"Re: {orig_subj}" if not orig_subj.startswith("Re:") else orig_subj

        try:
            draft_text = tm.fill_followup(
                followup_num,
                company=_clean_company_name(name),
                trade=trade,
                trade_plural=_trade_plural(trade),
                area=area or "your area",
                hook=orig_hook,
                sent_date=sent_date,
            )
        except ValueError as exc:
            errors.append(f"{name}: template error — {exc}")
            continue

        send_slot = next_send_slot(already_scheduled)
        already_scheduled.append(send_slot)  # reserve slot before next iteration

        if not dry_run:
            try:
                body_html = build_html_body(draft_text, sig_html)
                send_queue.enqueue(email, subject, draft_text, body_html, send_slot)
                col_sched = f"followup{followup_num}_scheduled_at"
                col_sent  = f"followup{followup_num}_sent_at"
                update_lead_pipeline(
                    lead["_place_id"],
                    **{col_sched: send_slot.isoformat(), col_sent: send_slot.isoformat()},
                )
                results.append((name, send_slot))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        else:
            results.append((name, send_slot))

    # Summary
    click.echo(f"\n{'[dry-run] Would schedule' if dry_run else 'Scheduled'} {len(results)} follow-up(s):\n")
    for name, slot in results:
        click.echo(f"  {name:<38}  {slot.strftime('%a %d %b at %H:%M')}")

    if errors:
        click.echo(f"\n{len(errors)} error(s):")
        for err in errors:
            click.echo(f"  {err}")


def _which_followup_slot(lead: dict) -> tuple[str, str]:
    """Return ('follow-up 1'|'follow-up 2', due_date_str) for whichever slot is next."""
    if lead.get("followup1_sent_at") is None and lead.get("followup1_due"):
        return "follow-up 1", lead["followup1_due"]
    return "follow-up 2", lead.get("followup2_due", "")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status() -> None:
    """Print a pipeline summary: counts, breakdowns, and upcoming follow-ups."""
    init_db()
    s = get_status_summary()

    click.echo("\n" + "=" * 50)
    click.echo("  TRADE LEADS - PIPELINE STATUS")
    click.echo("=" * 50)
    click.echo(f"\n  Total leads: {s['total']}\n")

    # Trade breakdown
    if s["trade_counts"]:
        parts = "  |  ".join(
            f"{t} {c}" for t, c in sorted(s["trade_counts"].items(), key=lambda x: -x[1])
        )
        click.echo(f"  By trade : {parts}")

    # Area breakdown
    if s["area_counts"]:
        top_areas = sorted(s["area_counts"].items(), key=lambda x: -x[1])[:8]
        parts = "  |  ".join(f"{a} {c}" for a, c in top_areas)
        click.echo(f"  By area  : {parts}")

    # Pipeline counts
    sc = s["status_counts"]
    click.echo("\n  Pipeline:")
    _status_line("Not contacted",    sc.get("not_contacted", 0))
    _status_line("Drafted",          sc.get("drafted", 0))
    _status_line("Emailed",          sc.get("emailed", 0))
    _status_line("Follow-up 1 due",  s["followup1_due_count"], warn=s["followup1_due_count"] > 0)
    _status_line("Follow-up 2 due",  s["followup2_due_count"], warn=s["followup2_due_count"] > 0)
    _status_line("Responded",        sc.get("responded", 0))
    _status_line("Closed",           sc.get("closed", 0))

    # Next follow-ups
    if s["next_followups"]:
        click.echo("\n  Upcoming follow-ups:")
        click.echo(f"  {'ID':>4}  {'Business':<28}  {'Slot':<12}  {'Due'}")
        click.echo("  " + "-" * 56)
        for fu in s["next_followups"]:
            click.echo(f"  {fu['rowid']:>4}  {fu['name']:<28}  {fu['slot']:<12}  {fu['due']}")

    # Next-action hint
    if s["followup1_due_count"] + s["followup2_due_count"] > 0:
        click.echo("\n  Next: trade-leads followup")
    elif sc.get("drafted", 0) > 0:
        click.echo("\n  Next: trade-leads send --id <ID>")
    elif sc.get("not_contacted", 0) > 0:
        click.echo("\n  Next: trade-leads draft")
    else:
        click.echo("\n  Next: trade-leads search  (no uncontacted leads remaining)")

    click.echo()


def _status_line(label: str, count: int, warn: bool = False) -> None:
    flag = "  << due / overdue" if warn and count > 0 else ""
    click.echo(f"    {label:<22} {count:>4}{flag}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@cli.command(name="list")
@click.option("--trade", default=None, help="Filter by trade type")
@click.option("--status", "filter_status", default=None, help="Filter by pipeline status")
def list_leads(trade: str | None, filter_status: str | None) -> None:
    """List all leads grouped by area."""
    init_db()
    rows = get_all_leads_with_pipeline()

    if not rows:
        click.echo("No leads found. Run 'trade-leads search' first.")
        return

    # Apply filters
    if trade:
        rows = [r for r in rows if r.get("trade", "").lower() == trade.lower()]
    if filter_status:
        rows = [r for r in rows if (r.get("status") or "not_contacted") == filter_status]

    if not rows:
        click.echo("No leads match the given filters.")
        return

    # Group by area
    groups: dict[str, list[dict]] = {}
    for row in rows:
        area = _parse_area(row.get("address", "")) or "Unknown area"
        groups.setdefault(area, []).append(row)

    total = len(rows)
    click.echo(f"\n{total} lead{'s' if total != 1 else ''} across {len(groups)} area{'s' if len(groups) != 1 else ''}\n")

    for area in sorted(groups):
        area_leads = groups[area]
        click.echo(f"  {area}  ({len(area_leads)})")
        click.echo(f"  {'-' * 74}")
        click.echo(f"  {'ID':>4}  {'Name':<28}  {'Sc/3':>4}  {'Contact':<30}  {'Status'}")
        for lead in sorted(area_leads, key=lambda r: -r.get("missed_call_score", 0)):
            rowid = lead["_rowid"]
            name = lead.get("name", "")[:27]
            score = lead.get("missed_call_score", 0)
            score_flag = "!" if score >= 2 else " "
            email = lead.get("email", "")
            phone = lead.get("phone", "")
            ev = lead.get("email_validation_status", "")
            if email:
                contact = email[:28] + ("+" if len(email) > 28 else "")
                ev_badge = f" [{ev}]" if ev and ev not in ("valid", "") else ""
                contact_display = contact + ev_badge
            elif phone:
                contact_display = f"tel: {phone}"
            else:
                contact_display = "(no contact)"
            status = lead.get("status") or "not_contacted"
            website = lead.get("website", "")
            click.echo(f"  {rowid:>4}  {name:<28}  {score}{score_flag}  {contact_display:<30}  {status}")
            if website:
                click.echo(f"        {website}")
        click.echo()


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--output", default="leads.csv", show_default=True, help="Output CSV file path")
def export(output: str) -> None:
    """Export all stored leads (including pipeline data) to CSV."""
    init_db()
    rows = get_all_leads_with_pipeline()

    if not rows:
        click.echo("No leads found. Run 'trade-leads search' first.")
        return

    path = export_csv_full(rows, output)
    click.echo(f"Exported {len(rows)} leads to {path}")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be sent without actually sending")
def dispatch(dry_run: bool) -> None:
    """Send any queued drafts whose scheduled send time has arrived."""
    init_db()

    initial = get_initial_drafts_due()
    followups = get_followup_drafts_due()

    if not initial and not followups:
        click.echo("No emails due to send right now.")
        return

    total = len(initial) + len(followups)
    click.echo(f"{total} email(s) due ({len(initial)} initial, {len(followups)} follow-up).")

    if not gmail_is_configured():
        raise click.ClickException("Gmail not configured.")

    service = gmail_authenticate()
    sent: list[str] = []
    errors: list[str] = []

    for lead in initial:
        name     = lead.get("name", "")
        draft_id = lead.get("gmail_draft_id")
        place_id = lead["_place_id"]
        if dry_run:
            sent.append(f"{name} (initial)")
            continue
        try:
            gmail_send_draft(service, draft_id)
            now = _now_iso()
            fu1, fu2 = followup_due_dates(now)
            update_lead_pipeline(
                place_id,
                status="emailed",
                sent_at=now,
                followup1_due=fu1,
                followup2_due=fu2,
            )
            sent.append(f"{name} (initial)")
        except Exception as exc:
            errors.append(f"{name} (initial): {exc}")

    for lead in followups:
        name     = lead.get("name", "")
        place_id = lead["_place_id"]
        now      = _now_iso()

        # Determine which follow-up slot is ready
        if lead.get("followup1_draft_id") and lead.get("followup1_scheduled_at") and not lead.get("followup1_sent_at"):
            draft_id = lead["followup1_draft_id"]
            col_sent = "followup1_sent_at"
            label    = "follow-up 1"
        else:
            draft_id = lead["followup2_draft_id"]
            col_sent = "followup2_sent_at"
            label    = "follow-up 2"

        if dry_run:
            sent.append(f"{name} ({label})")
            continue
        try:
            gmail_send_draft(service, draft_id)
            update_lead_pipeline(place_id, **{col_sent: now})
            sent.append(f"{name} ({label})")
        except Exception as exc:
            errors.append(f"{name} ({label}): {exc}")

    prefix = "[dry-run] Would send" if dry_run else "Sent"
    for item in sent:
        click.echo(f"  {prefix}: {item}")
    if errors:
        click.echo(f"\n{len(errors)} error(s):")
        for err in errors:
            click.echo(f"  {err}")


# ---------------------------------------------------------------------------
# lookup  (Companies House diagnostic)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("company_name")
def lookup(company_name: str) -> None:
    """Look up a director name on Companies House and show every step."""
    import difflib, re as _re, httpx as _httpx

    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    if not ch_key:
        api_keys = sorted(k for k in os.environ if "KEY" in k.upper() or "API" in k.upper())
        click.echo(f"API/KEY env vars currently loaded: {api_keys}")
        raise click.ClickException("COMPANIES_HOUSE_API_KEY not set — check the name above")

    _BASE = "https://api.company-information.service.gov.uk"
    auth = (ch_key, "")

    def _norm(n: str) -> str:
        return _re.sub(r"\s+", " ", n.replace("&", "and")).strip()

    def _strip(n: str) -> str:
        return _re.sub(r"\b(limited|ltd\.?|llp|plc)\b\.?$", "", n, flags=_re.IGNORECASE).strip()

    def _sim(a: str, b: str) -> float:
        def _clean(s: str) -> str:
            s = _re.sub(r"\b(limited|ltd\.?|llp|plc)\b\.?", "", s.lower().replace("&", "and"), flags=_re.IGNORECASE)
            return s.strip()
        return difflib.SequenceMatcher(None, _clean(a), _clean(b)).ratio()

    queries = [_norm(company_name)]
    short = _strip(_norm(company_name))
    if short != queries[0]:
        queries.append(short)

    company_number = None
    for q in queries:
        click.echo(f"\nSearching CH for: '{q}'")
        try:
            resp = _httpx.get(f"{_BASE}/search/companies", params={"q": q, "items_per_page": 20}, auth=auth, timeout=8)
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except Exception as exc:
            click.echo(f"  ERROR: {exc}")
            continue

        if not items:
            click.echo("  No results returned.")
            continue

        click.echo(f"  {len(items)} result(s):")
        for item in items[:8]:
            sim = _sim(item.get("title", ""), company_name)
            status = item.get("company_status", "?")
            click.echo(f"    [{status:8}] {item.get('title',''):40}  similarity {sim:.2f}  #{item.get('company_number')}")

        active = [i for i in items if i.get("company_status") == "active"]
        if not active:
            click.echo("  None are active — trying next query.")
            continue

        best = max(active, key=lambda i: _sim(i.get("title", ""), company_name))
        score = _sim(best.get("title", ""), company_name)
        click.echo(f"\n  Best active match: '{best.get('title')}' (similarity {score:.2f})")
        if score < 0.4:
            click.echo("  Score below 0.4 threshold — skipping.")
            continue
        company_number = best["company_number"]
        break

    if not company_number:
        click.echo("\nNo suitable company found on Companies House.")
        return

    click.echo(f"\nFetching officers for company #{company_number}...")
    try:
        resp = _httpx.get(f"{_BASE}/company/{company_number}/officers", params={"items_per_page": 50}, auth=auth, timeout=8)
        resp.raise_for_status()
        officers = resp.json().get("items", [])
    except Exception as exc:
        click.echo(f"  ERROR: {exc}")
        return

    if not officers:
        click.echo("  No officers returned.")
        return

    _DIR_ROLES = {"director", "corporate-director", "nominee-director"}
    click.echo(f"  {len(officers)} officer(s):")
    for o in officers:
        role = o.get("officer_role", "?")
        name = o.get("name", "?")
        resigned = o.get("resigned_on", "")
        flag = " [RESIGNED]" if resigned else ""
        active_dir = (not resigned) and role.lower() in _DIR_ROLES
        marker = " <-- ACTIVE DIRECTOR" if active_dir else ""
        click.echo(f"    [{role:25}] {name}{flag}{marker}")

    click.echo()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli()
