"""Import prospects that another system already found and measured.

An operator often has a better list than discovery could build: a CRM, an
audit of every business in a town, last year's customers. ``import_records``
takes those rows as plain dicts (from ``harvey import <file>`` or from the
cabin connector, harvey/integrations/cabin.py) and turns each into a company
plus its general-inbox contact, tagged with the product it is offered.

Imported facts are kept as the prospect's research notes, so the Writer
cites what the operator measured ("their homepage takes 1.4 s to answer")
and nothing else. A row is never trusted to override a stop: a contact who
opted out, replied or is on the do-not-contact list stays as they are.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from harvey.config import live_do_not_contact
from harvey.gate import do_not_contact_domain_match, do_not_contact_match
from harvey.models.company import Company
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect

logger = logging.getLogger("harvey.importer")

IMPORT_SOURCE_PREFIX = "import:"
MAX_FACTS = 12
MAX_FACT_CHARS = 240
MAX_FIELD_CHARS = 200
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,79}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
IMPORTABLE_EMAIL_STATUSES = ("verified", "risky", "guess")
# Statuses an import may still re-route to another product: nobody has been
# written to yet.
REROUTABLE_STATUSES = ("new", "write_failed")


@dataclass
class ImportResult:
    created: dict[str, str] = field(default_factory=dict)      # external_id -> prospect id
    updated: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)      # external_id -> reason
    errors: dict[str, str] = field(default_factory=dict)       # external_id/row -> problem

    def summary(self) -> dict:
        return {"created": len(self.created), "updated": len(self.updated),
                "skipped": len(self.skipped), "errors": len(self.errors)}

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated,
                "skipped": self.skipped, "errors": self.errors, "counts": self.summary()}


def _text(value, limit=MAX_FIELD_CHARS) -> str:
    """One line of plain text, at most ``limit`` characters."""
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _domain(record: dict) -> str:
    raw = _text(record.get("domain") or record.get("website"), 300).lower()
    raw = re.sub(r"^[a-z]+://", "", raw).split("/")[0].split(":")[0]
    raw = raw[4:] if raw.startswith("www.") else raw
    return raw if DOMAIN_RE.match(raw) else ""


def _facts(record: dict) -> list[str]:
    """Measured facts as short lines: a list of strings or a {label: value} map."""
    raw = record.get("facts") or []
    if isinstance(raw, dict):
        items = [f"{_text(k, 60)}: {_text(v, MAX_FACT_CHARS)}" for k, v in raw.items()
                 if _text(v)]
    elif isinstance(raw, (list, tuple)):
        items = [_text(v, MAX_FACT_CHARS) for v in raw if _text(v)]
    else:
        items = []
    return items[:MAX_FACTS]


def research_notes(facts: list[str], notes: str = "") -> str:
    lines = []
    if facts:
        lines.append("Measured by us on their website and listings: " + "; ".join(facts) + ".")
    if notes:
        lines.append(notes)
    return " ".join(lines)


def parse_record(record, product_keys) -> tuple[dict | None, str]:
    """A cleaned record, or (None, why it can't be imported)."""
    if not isinstance(record, dict):
        return None, "not an object"
    external_id = _text(record.get("external_id"), 80)
    if not EXTERNAL_ID_RE.match(external_id):
        return None, "external_id is missing or has characters other than letters, digits, :._-"
    company = _text(record.get("company"))
    if not company:
        return None, "company is missing"
    email = _text(record.get("email"), 254).lower()
    if email and not EMAIL_RE.match(email):
        return None, f"email {email!r} is not an address"
    email_status = _text(record.get("email_status"), 20).lower() or "risky"
    if email_status not in IMPORTABLE_EMAIL_STATUSES:
        return None, f"email_status must be one of {', '.join(IMPORTABLE_EMAIL_STATUSES)}"
    product_key = _text(record.get("product_key"), 40).lower()
    if product_key and product_key not in product_keys:
        return None, f"product_key {product_key!r} is not one of the configured products"
    domain = _domain(record)
    if not (email or domain or _text(record.get("phone"))):
        return None, "needs an email, a website or a phone to be reachable"
    return {
        "external_id": external_id,
        "company": company,
        "domain": domain,
        "website": f"https://{domain}" if domain else "",
        "email": email,
        "email_status": email_status if email else "",
        "phone": _text(record.get("phone"), 40),
        "industry": _text(record.get("industry"), 80),
        "location": _text(record.get("location"), 120),
        "product_key": product_key,
        "notes": research_notes(_facts(record), _text(record.get("notes"), 600)),
    }, ""


async def import_records(state, config, records) -> ImportResult:
    """Add each record as a company and its general-inbox contact."""
    result = ImportResult()
    keys = set(config.product_keys()) if hasattr(config, "product_keys") else set()
    blocked = live_do_not_contact(config)
    for n, record in enumerate(records or []):
        clean, problem = parse_record(record, keys)
        if clean is None:
            label = (_text(record.get("external_id"), 80) if isinstance(record, dict) else "") \
                or f"row {n + 1}"
            result.errors[label] = problem
            continue
        ext = clean["external_id"]
        hit = (do_not_contact_match(clean["email"], blocked) if clean["email"] else "") or \
            (do_not_contact_domain_match(clean["domain"], blocked) if clean["domain"] else "")
        if hit:
            result.skipped[ext] = f"on the do-not-contact list ({hit})"
            continue
        try:
            await _import_one(state, clean, result)
        except Exception as e:  # one bad row never stops the batch
            logger.warning(f"Import: {ext} failed: {e}")
            result.errors[ext] = f"could not be saved: {e}"
    if result.created or result.updated:
        await state.log_action(action_type="import", agent="importer", details=result.summary())
    return result


async def _import_one(state, clean: dict, result: ImportResult) -> None:
    ext = clean["external_id"]
    source_url = IMPORT_SOURCE_PREFIX + ext

    existing = await state.get_prospect_by_source_url(source_url)
    if existing is None and clean["email"]:
        existing = await state.get_prospect_by_email(clean["email"])
    if existing is not None:
        if existing.status not in REROUTABLE_STATUSES:
            result.skipped[ext] = f"already {existing.status}"
            return
        await state.update_prospect_fields(existing.id, {
            "product_key": clean["product_key"],
            "personalization_notes": clean["notes"] or existing.personalization_notes,
        })
        result.updated[ext] = existing.id
        return

    company_id = await state.add_company(Company(
        name=clean["company"], domain=clean["domain"], website=clean["website"],
        industry=clean["industry"], location=clean["location"], phone=clean["phone"],
        source="import", source_url=source_url, external_id=source_url,
        notes=clean["notes"],
    ))
    if clean["domain"] and await state.get_contacts_for_company(company_id):
        # Someone at this company is already in Harvey: one conversation per company.
        result.skipped[ext] = "another contact at this company is already in Harvey"
        return
    prospect = Prospect(
        company_id=company_id, company=clean["company"], industry=clean["industry"],
        email=clean["email"], email_status=clean["email_status"],
        email_verified=clean["email_status"] == "verified",
        phone=clean["phone"], title="General inbox", source=COMPANY_INBOX_SOURCE,
        source_url=source_url, personalization_notes=clean["notes"],
        product_key=clean["product_key"],
    )
    pid = await state.add_prospect(prospect)
    stored = await state.get_prospect(pid)
    if stored is not None and stored.source_url != source_url:
        result.skipped[ext] = "this address is already in Harvey"
        return
    result.created[ext] = pid


def read_records(path) -> list:
    """Records from a .json file (a list) or a .jsonl file (one per line)."""
    text = open(path, encoding="utf-8").read()
    if str(path).endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else data.get("records", [])
