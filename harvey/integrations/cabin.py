"""The cabin: run Harvey from another system's dashboard (a CRM or ERP).

Harvey's own dashboard only answers on localhost. An operator who already
works from a company dashboard wants to feed Harvey from it and see and
steer the outreach there. Harvey keeps its rule of never listening on the
network: once a cycle it makes ONE outbound HTTPS call,

    POST <cabin.url>/sync    Authorization: Bearer <CABIN_TOKEN>

sending what happened since the last call, and taking back what to do next.

Request body::

    {"version": 1,
     "events":  [{"id", "type", "at", "external_id", "email", ...}],
     "outbox":  [{"id", "external_id", "to", "subject", "body", "kind", "step",
                  "product_key", "held", "created_at"}],     # all pending_review
     "import_results": {...} | null,                         # of the last records
     "status":  {"sending_paused": "...", "pending": N}}

Response body (every key optional)::

    {"records":   [<import rows, see harvey/importer.py>],
     "decisions": [{"outbox_id": "...", "decision": "approve" | "reject"}],
     "control":   {"sending_paused": true | false}}

The events cursor only moves forward after a 2xx, so a lost call is resent.
Decisions are applied through the same state calls as Harvey's own Outbox
buttons. The cabin may pause sending at any time, but may only resume a
pause it set itself: a stop Harvey made on its own (bounce rate) is resumed
deliberately, in Harvey.
"""

from __future__ import annotations

import json
import logging

import httpx

from harvey.importer import IMPORT_SOURCE_PREFIX, import_records
from harvey.state import HELD_FOR_REVIEW

logger = logging.getLogger("harvey.cabin")

EVENTS_CURSOR_KEY = "cabin_events_cursor"
PENDING_IMPORT_RESULTS_KEY = "cabin_import_results"
CABIN_PAUSE_REASON = "paused from the cabin"
SENDING_PAUSED_KEY = "sending_paused"
EVENT_TYPES = ("email_sent", "reply", "reply_queued", "opt_out", "bounce", "escalation",
               "manual_send", "write_failed", "reply_after_opt_out")
MAX_EVENTS = 500
MAX_OUTBOX = 200
MAX_RECORDS = 500
MAX_DECISIONS = 500
TIMEOUT_S = 30


class CabinError(Exception):
    pass


def cabin_enabled(config, env) -> bool:
    cabin = getattr(config, "cabin", None)
    return bool(cabin and cabin.enabled and cabin.url and getattr(env, "cabin_token", ""))


async def _external_ids(state, prospect_ids: set[str], emails: set[str]) -> tuple[dict, dict]:
    """external_id by prospect id and by email, for imported prospects."""
    by_id, by_email = {}, {}
    for pid in prospect_ids:
        p = await state.get_prospect(pid)
        if p and (p.source_url or "").startswith(IMPORT_SOURCE_PREFIX):
            by_id[pid] = p.source_url[len(IMPORT_SOURCE_PREFIX):]
            if p.email:
                by_email[p.email] = by_id[pid]
    for email in emails - set(by_email):
        p = await state.get_prospect_by_email(email)
        if p and (p.source_url or "").startswith(IMPORT_SOURCE_PREFIX):
            by_email[email] = p.source_url[len(IMPORT_SOURCE_PREFIX):]
    return by_id, by_email


def _event_email(details: dict) -> str:
    return (details.get("prospect_email") or details.get("to") or details.get("prospect")
            or "").strip().lower()


async def collect_events(state) -> tuple[list[dict], str]:
    """Actions since the cursor, as cabin events, and the new cursor."""
    cursor = await state.get_setting(EVENTS_CURSOR_KEY) or ""
    rows = await state.get_actions_after(cursor, EVENT_TYPES, MAX_EVENTS)
    if not rows:
        return [], cursor
    emails = set()
    ids = set()
    parsed = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        parsed.append((row, details))
        if _event_email(details):
            emails.add(_event_email(details))
        if details.get("prospect_id"):
            ids.add(details["prospect_id"])
    by_id, by_email = await _external_ids(state, ids, emails)
    events = []
    for row, details in parsed:
        email = _event_email(details)
        event = {
            "id": row["id"], "type": row["action_type"], "at": row["created_at"],
            "external_id": by_id.get(details.get("prospect_id", "")) or by_email.get(email, ""),
            "email": email,
        }
        for key in ("intent", "step", "kind", "reason", "conversation_id"):
            if key in details:
                event[key] = details[key]
        preview = details.get("reply_preview") or details.get("message") or ""
        if preview:
            event["preview"] = str(preview)[:500]
        events.append(event)
    last = rows[-1]
    return events, f"{last['created_at']}|{last['id']}"


async def collect_outbox(state) -> list[dict]:
    items = await state.get_outbox(status="pending_review", limit=MAX_OUTBOX)
    by_id, _ = await _external_ids(state, {i.get("prospect_id", "") for i in items if i.get("prospect_id")},
                                   set())
    out = []
    for item in items:
        out.append({
            "id": item["id"], "external_id": by_id.get(item.get("prospect_id", ""), ""),
            "to": item.get("to_email", ""), "subject": item.get("subject", ""),
            "body": item.get("body", ""), "kind": item.get("kind", ""),
            "step": item.get("step"),
            "held": (item.get("error") or "").startswith(HELD_FOR_REVIEW),
            "created_at": item.get("created_at", ""),
        })
    return out


async def apply_decisions(state, decisions) -> dict:
    applied = {"approved": 0, "rejected": 0, "ignored": 0}
    for d in (decisions or [])[:MAX_DECISIONS]:
        if not isinstance(d, dict):
            applied["ignored"] += 1
            continue
        item_id = str(d.get("outbox_id") or "")
        decision = d.get("decision")
        item = await state.get_outbox_item(item_id) if item_id else None
        if not item or item.get("status") != "pending_review":
            applied["ignored"] += 1
        elif decision == "approve" and await state.approve_outbox(item_id):
            applied["approved"] += 1
        elif decision == "reject" and await state.reject_outbox_item(item_id):
            applied["rejected"] += 1
        else:
            applied["ignored"] += 1
    return applied


async def apply_control(state, control) -> str:
    """Pause or resume sending as the cabin asks; returns what changed."""
    if not isinstance(control, dict) or "sending_paused" not in control:
        return ""
    paused_now = await state.get_setting(SENDING_PAUSED_KEY) or ""
    want = control.get("sending_paused")
    if want is True and not paused_now:
        await state.set_setting(SENDING_PAUSED_KEY, CABIN_PAUSE_REASON)
        return "paused"
    if want is False and paused_now == CABIN_PAUSE_REASON:
        await state.set_setting(SENDING_PAUSED_KEY, "")
        return "resumed"
    if want is False and paused_now:
        logger.warning(f"Cabin asked to resume, but Harvey paused itself ({paused_now}); "
                       "resume it in Harvey.")
    return ""


async def sync(state, config, env, client: httpx.AsyncClient | None = None) -> dict:
    """One round trip with the cabin. Returns a summary for the log."""
    url = config.cabin.url.rstrip("/") + "/sync"
    events, new_cursor = await collect_events(state)
    pending_results = await state.get_setting(PENDING_IMPORT_RESULTS_KEY) or ""
    body = {
        "version": 1,
        "events": events,
        "outbox": await collect_outbox(state),
        "import_results": json.loads(pending_results) if pending_results else None,
        "status": {"sending_paused": await state.get_setting(SENDING_PAUSED_KEY) or "",
                   "product_keys": config.product_keys()},
    }
    headers = {"Authorization": f"Bearer {env.cabin_token}", "Content-Type": "application/json"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)
    try:
        resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise CabinError(f"cabin unreachable: {e}") from e
    finally:
        if own:
            await client.aclose()
    if not 200 <= resp.status_code < 300:
        raise CabinError(f"cabin answered {resp.status_code}")
    try:
        data = resp.json() if resp.content else {}
    except ValueError as e:
        raise CabinError("cabin answered with something that isn't JSON") from e
    if not isinstance(data, dict):
        raise CabinError("cabin answer must be a JSON object")

    # Delivered: move the cursor and forget the results we reported.
    if new_cursor:
        await state.set_setting(EVENTS_CURSOR_KEY, new_cursor)
    if pending_results:
        await state.delete_setting(PENDING_IMPORT_RESULTS_KEY)

    summary = {"events_sent": len(events), "outbox_sent": len(body["outbox"])}
    summary["control"] = await apply_control(state, data.get("control"))
    summary.update(await apply_decisions(state, data.get("decisions")))
    records = data.get("records") or []
    if isinstance(records, list) and records:
        result = await import_records(state, config, records[:MAX_RECORDS])
        await state.set_setting(PENDING_IMPORT_RESULTS_KEY, json.dumps(result.as_dict()))
        summary["imported"] = result.summary()
    return summary
