"""Importing prospects another system found and measured."""

import json

import pytest
import pytest_asyncio

from harvey.config import HarveyConfig
from harvey.importer import import_records, read_records
from harvey.state import StateManager
from tests.test_multi_product import BASE, HOSTING, MAPS


def _cfg(dnc=()):
    return HarveyConfig(**BASE, products=[HOSTING, MAPS],
                        compliance={"do_not_contact": list(dnc)})


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "imp.db"))
    await s.init_db()
    return s


def row(n=1, **kw):
    base = {"external_id": f"erp:{n}", "company": f"Shop {n}", "website": f"https://www.shop{n}.example/",
            "email": f"info@shop{n}.example", "product_key": "hosting",
            "facts": {"Homepage response": "1.4 s", "ChatGPT crawler": "blocked"}}
    return {**base, **kw}


@pytest.mark.asyncio
async def test_a_row_becomes_a_company_and_its_inbox_tagged_with_its_product(state):
    result = await import_records(state, _cfg(), [row()])
    assert result.summary() == {"created": 1, "updated": 0, "skipped": 0, "errors": 0}
    p = await state.get_prospect(result.created["erp:1"])
    assert p.product_key == "hosting" and p.is_company_inbox and p.is_valid()
    assert p.email == "info@shop1.example" and p.email_status == "risky"
    assert "1.4 s" in p.personalization_notes and "blocked" in p.personalization_notes
    company = await state.get_company(p.company_id)
    assert company.domain == "shop1.example" and company.name == "Shop 1"


@pytest.mark.asyncio
async def test_importing_again_reroutes_a_contact_nobody_has_written_to(state):
    first = await import_records(state, _cfg(), [row()])
    again = await import_records(state, _cfg(), [row(product_key="maps")])
    assert again.updated == first.created
    assert (await state.get_prospect(first.created["erp:1"])).product_key == "maps"


@pytest.mark.asyncio
async def test_an_import_never_overrides_a_stop(state):
    first = await import_records(state, _cfg(), [row()])
    pid = first.created["erp:1"]
    await state.update_prospect_status(pid, "opted_out")
    again = await import_records(state, _cfg(), [row(product_key="maps")])
    assert again.skipped == {"erp:1": "already opted_out"}
    p = await state.get_prospect(pid)
    assert p.status == "opted_out" and p.product_key == "hosting"


@pytest.mark.asyncio
async def test_do_not_contact_rows_are_skipped(state):
    result = await import_records(state, _cfg(dnc=["shop2.example"]), [row(1), row(2)])
    assert list(result.created) == ["erp:1"]
    assert "do-not-contact" in result.skipped["erp:2"]


@pytest.mark.asyncio
async def test_one_contact_per_company(state):
    result = await import_records(state, _cfg(), [
        row(1), row(9, website="shop1.example", email="sales@shop1.example")])
    assert list(result.created) == ["erp:1"]
    assert "already in Harvey" in result.skipped["erp:9"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad, problem", [
    ({"external_id": "erp 1"}, "external_id"),
    ({"company": ""}, "company"),
    ({"email": "not-an-address"}, "not an address"),
    ({"email_status": "invalid"}, "email_status"),
    ({"product_key": "kit-digital"}, "not one of the configured products"),
    ({"email": "", "website": "", "phone": ""}, "reachable"),
])
async def test_bad_rows_are_reported_not_imported(state, bad, problem):
    result = await import_records(state, _cfg(), [row(**bad), row(2)])
    assert list(result.created) == ["erp:2"]
    assert problem in next(iter(result.errors.values()))


@pytest.mark.asyncio
async def test_a_business_with_only_a_phone_is_kept_for_other_channels(state):
    result = await import_records(state, _cfg(), [
        row(email="", website="", phone="+34 600 000 000")])
    p = await state.get_prospect(result.created["erp:1"])
    assert p.email == "" and p.phone == "+34 600 000 000"


def test_files_can_be_json_or_jsonl(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([row(1)]), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(json.dumps(row(1)) + "\n\n" + json.dumps(row(2)) + "\n",
                                      encoding="utf-8")
    assert len(read_records(tmp_path / "a.json")) == 1
    assert len(read_records(tmp_path / "b.jsonl")) == 2
