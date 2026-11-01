"""Several products: each prospect carries the key of the product it is
offered, and is written to and answered about that product only."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.brain as brain_module
from harvey.agents.writer import Writer, product_rules_block
from harvey.brain import Brain
from harvey.config import HarveyConfig, ProductConfig, product_for
from harvey.models.campaign import Campaign
from harvey.models.prospect import Prospect
from harvey.state import StateManager

BASE = {
    "persona": {"name": "Dana", "company": "Acme Web", "role": "Sales", "email": "dana@example.com",
                "linkedin": "", "tone": "plain"},
    "product": {"name": "Main", "description": "The main thing", "pricing": "ask",
                "key_benefits": [], "objection_responses": {}},
    "icp": {"industries": ["retail"], "company_size": "1-10", "titles": ["Owner"],
            "geography": ["Example City"]},
}
HOSTING = {"key": "hosting", "name": "Managed hosting", "description": "Fast EU hosting",
           "pricing": "from what you pay today", "key_benefits": ["fast"],
           "objection_responses": {"price": "Start from today's bill."},
           "rules": ["Never name the data-centre provider."]}
MAPS = {"key": "maps", "name": "Maps listing", "description": "Better map ranking",
        "pricing": "99 + 35/month", "key_benefits": [], "objection_responses": {}}


def _cfg(**extra):
    return HarveyConfig(**BASE, **extra)


# ── Config ──

def test_a_prospect_gets_its_own_product_or_the_main_one():
    cfg = _cfg(products=[HOSTING, MAPS])
    assert cfg.product_for("hosting").name == "Managed hosting"
    assert cfg.product_for("MAPS").name == "Maps listing"
    assert cfg.product_for("").name == "Main"
    assert cfg.product_for("unknown").name == "Main"
    assert cfg.product_keys() == ["hosting", "maps"]


def test_products_need_unique_valid_keys():
    with pytest.raises(ValidationError):
        _cfg(products=[{**HOSTING, "key": ""}])
    with pytest.raises(ValidationError):
        _cfg(products=[HOSTING, {**MAPS, "key": "hosting"}])
    with pytest.raises(ValidationError):
        _cfg(products=[{**HOSTING, "key": "Hosting IA!"}])


def test_a_config_without_products_still_works():
    assert product_for(SimpleNamespace(product="only"), "hosting") == "only"
    assert _cfg().products == []


def test_rules_become_a_prompt_section():
    block = product_rules_block(ProductConfig(**HOSTING))
    assert "Never name the data-centre provider." in block
    assert product_rules_block(ProductConfig(**MAPS)) == ""


# ── State ──

@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "mp.db"))
    await s.init_db()
    return s


@pytest.mark.asyncio
async def test_product_key_is_stored_on_prospects_and_campaigns(state):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Ana Co",
        email="ana@example.com", email_status="verified", product_key="hosting"))
    assert (await state.get_prospect(pid)).product_key == "hosting"
    await state.add_campaign(Campaign(id="", name="c", product_key="maps", status="draft"))
    [campaign] = await state.get_campaigns_by_status("draft")
    assert campaign.product_key == "maps"


# ── Skills ──

def test_each_product_is_sold_from_its_own_knowledge(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "products").mkdir(parents=True)
    (skills / "product_knowledge.md").write_text("MAIN FACTS", encoding="utf-8")
    (skills / "products" / "hosting.md").write_text("HOSTING FACTS", encoding="utf-8")
    monkeypatch.setattr(brain_module, "SKILLS_DIR", skills)
    brain = Brain.__new__(Brain)
    assert "MAIN FACTS" in brain.load_skills_for_agent("writer")
    hosting = brain.load_skills_for_agent("writer", "hosting")
    assert "HOSTING FACTS" in hosting and "MAIN FACTS" not in hosting
    # No file for the product, or a key that isn't one: the main knowledge.
    assert "MAIN FACTS" in brain.load_skills_for_agent("writer", "maps")
    assert "MAIN FACTS" in brain.load_skills_for_agent("writer", "../secrets")


# ── Writer ──

class RecordingBrain:
    def __init__(self):
        self.prompts, self.skill_calls = [], []

    def load_skills_for_agent(self, name, product_key=""):
        self.skill_calls.append(product_key)
        return f"SKILLS[{product_key or 'main'}]"

    def load_prompt(self, name, **kw):
        return f"Product: {kw.get('product_name')} / {kw.get('product_pricing')}"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}",
                 "delay_days": 0 if n == 1 else 3} for n in (1, 2, 3)]


@pytest.mark.asyncio
async def test_writer_writes_one_campaign_per_product(state):
    cfg = _cfg(products=[HOSTING, MAPS])
    cfg.channels.email.provider = "instantly"     # one template per batch, no per-person call
    for name, key in (("Ana", "hosting"), ("Bea", "maps"), ("Cai", "")):
        await state.add_prospect(Prospect(
            first_name=name, last_name="Doe", title="Owner", company=f"{name} Co",
            industry="retail", email=f"{name.lower()}@example.com", email_status="verified",
            product_key=key))
    brain = RecordingBrain()
    await Writer(brain, state, cfg).run()

    campaigns = await state.get_campaigns_by_status("draft")
    assert sorted(c.product_key for c in campaigns) == ["", "hosting", "maps"]
    joined = "\n".join(brain.prompts)
    assert "Product: Managed hosting / from what you pay today" in joined
    assert "Product: Maps listing / 99 + 35/month" in joined
    assert "Product: Main / ask" in joined
    assert "SKILLS[hosting]" in joined and "SKILLS[maps]" in joined
    hosting_prompt = next(p for p in brain.prompts if "Managed hosting" in p)
    assert "Never name the data-centre provider." in hosting_prompt
    maps_prompt = next(p for p in brain.prompts if "Maps listing" in p)
    assert "Never name the data-centre provider." not in maps_prompt


# ── A product that always waits for a human ──

from harvey.config import approval_required  # noqa: E402
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign, seed_prospect  # noqa: E402


def test_a_product_can_require_approval_even_on_autopilot():
    cfg = _cfg(products=[{**HOSTING, "key": "clients", "require_approval": True}, MAPS])
    cfg.channels.email.require_approval = False
    assert approval_required(cfg, "clients")
    assert not approval_required(cfg, "maps")
    assert not approval_required(cfg, "")
    cfg.channels.email.require_approval = True
    assert approval_required(cfg, "maps")


@pytest.mark.asyncio
async def test_autopilot_still_stages_that_products_emails_for_review(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.product = type("CustomersOnly", (sender.config.product,), {"require_approval": True})
    await sender._run_native()
    assert provider.sent == []
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_an_autopilot_approval_for_that_product_goes_back_to_review(state):
    from datetime import datetime, timezone
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hola", body="Una nota corta. ¿Pregunta?",
        send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), status="approved",
        campaign_id="c1", step=2, approved_by="autopilot")
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.product = type("CustomersOnly", (sender.config.product,), {"require_approval": True})
    await sender._drain_due()
    assert provider.sent == []
    assert len(await state.get_outbox(status="pending_review")) == 1
