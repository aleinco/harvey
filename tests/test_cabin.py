"""The cabin: one outbound call a cycle to another system's dashboard."""

import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from harvey.config import CabinConfig, HarveyConfig
from harvey.integrations.cabin import (
    CABIN_PAUSE_REASON, EVENTS_CURSOR_KEY, CabinError, cabin_enabled, sync,
)
from harvey.state import StateManager
from tests.test_multi_product import BASE, HOSTING, MAPS

ENV = SimpleNamespace(cabin_token="t0ken")


def _cfg():
    return HarveyConfig(**BASE, products=[HOSTING, MAPS],
                        cabin={"enabled": True, "url": "https://erp.example/api/v1/harvey"})


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "cabin.db"))
    await s.init_db()
    return s


class FakeCabin:
    """Answers /sync like a dashboard would, and records what it was sent."""

    def __init__(self, answers=None, status=200):
        self.answers, self.status, self.calls = list(answers or []), status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                           "body": json.loads(request.content)})
        answer = self.answers.pop(0) if self.answers else {}
        return httpx.Response(self.status, json=answer)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _sync(state, cabin):
    async with cabin.client() as client:
        return await sync(state, _cfg(), ENV, client=client)


ROW = {"external_id": "erp:7", "company": "Shop 7", "website": "shop7.example",
       "email": "info@shop7.example", "product_key": "hosting", "facts": ["Homepage 1.4 s"]}


def test_enabled_only_with_url_and_token():
    assert cabin_enabled(_cfg(), ENV)
    assert not cabin_enabled(_cfg(), SimpleNamespace(cabin_token=""))
    assert not cabin_enabled(HarveyConfig(**BASE), ENV)


@pytest.mark.parametrize("bad", ["http://erp.example", "ftp://erp.example",
                                 "https://user:pw@erp.example"])
def test_cabin_url_must_be_https_without_credentials(bad):
    with pytest.raises(ValidationError):
        CabinConfig(url=bad)
    assert CabinConfig(url="http://127.0.0.1:8000/").url == "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_records_are_imported_and_their_results_reported_next_time(state):
    cabin = FakeCabin([{"records": [ROW]}, {}])
    first = await _sync(state, cabin)
    assert first["imported"]["created"] == 1
    assert cabin.calls[0]["url"] == "https://erp.example/api/v1/harvey/sync"
    assert cabin.calls[0]["auth"] == "Bearer t0ken"
    assert cabin.calls[0]["body"]["import_results"] is None
    await _sync(state, cabin)
    reported = cabin.calls[1]["body"]["import_results"]
    assert "erp:7" in reported["created"]
    await _sync(state, cabin)
    assert cabin.calls[2]["body"]["import_results"] is None   # reported once


@pytest.mark.asyncio
async def test_events_carry_the_cabins_own_id_and_go_out_once(state):
    await _sync(state, FakeCabin([{"records": [ROW]}]))
    await state.log_action("email_sent", "sender", {"to": "info@shop7.example", "step": 1,
                                                     "kind": "sequence"})
    await state.log_action("prospect", "scout", {})            # not a cabin event
    cabin = FakeCabin()
    await _sync(state, cabin)
    events = cabin.calls[0]["body"]["events"]
    assert [(e["type"], e["external_id"], e["step"]) for e in events] == [
        ("email_sent", "erp:7", 1)]
    await _sync(state, cabin)
    assert cabin.calls[1]["body"]["events"] == []


@pytest.mark.asyncio
async def test_a_failed_call_keeps_the_events_for_next_time(state):
    await state.log_action("opt_out", "handler", {"prospect_email": "x@example.com"})
    with pytest.raises(CabinError):
        await _sync(state, FakeCabin(status=503))
    assert not await state.get_setting(EVENTS_CURSOR_KEY)
    cabin = FakeCabin()
    await _sync(state, cabin)
    assert [e["type"] for e in cabin.calls[0]["body"]["events"]] == ["opt_out"]


async def _pending(state, pid, body="Hola, una pregunta corta.", step=1):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="info@shop7.example", subject="hola", body=body,
        send_at="2026-01-01T09:00:00", status="pending_review", campaign_id="c1",
        step=step, kind="sequence")


@pytest.mark.asyncio
async def test_pending_messages_go_to_the_cabin_and_its_decisions_come_back(state):
    first = await _sync(state, FakeCabin([{"records": [ROW]}]))
    pid = (await state.get_prospect_by_source_url("import:erp:7")).id
    approve, reject = await _pending(state, pid), await _pending(state, pid, "Otra.", step=2)
    cabin = FakeCabin([{"decisions": [
        {"outbox_id": approve, "decision": "approve"},
        {"outbox_id": reject, "decision": "reject"},
        {"outbox_id": "nope", "decision": "approve"},
        {"outbox_id": approve, "decision": "launch"}]}])
    summary = await _sync(state, cabin)
    sent = cabin.calls[0]["body"]["outbox"]
    assert {o["id"] for o in sent} == {approve, reject}
    assert all(o["external_id"] == "erp:7" for o in sent)
    assert (summary["approved"], summary["rejected"], summary["ignored"]) == (1, 1, 2)
    assert (await state.get_outbox_item(approve))["status"] == "approved"
    assert (await state.get_outbox_item(reject))["status"] == "rejected"
    assert first["imported"]["created"] == 1


@pytest.mark.asyncio
async def test_the_cabin_can_pause_and_resume_only_its_own_pause(state):
    await _sync(state, FakeCabin([{"control": {"sending_paused": True}}]))
    assert await state.get_setting("sending_paused") == CABIN_PAUSE_REASON
    await _sync(state, FakeCabin([{"control": {"sending_paused": False}}]))
    assert not await state.get_setting("sending_paused")
    # Harvey stopped itself (bounce rate): the cabin can't undo that.
    await state.set_setting("sending_paused", "bounce rate 6% over 5% limit")
    summary = await _sync(state, FakeCabin([{"control": {"sending_paused": False}}]))
    assert summary["control"] == ""
    assert await state.get_setting("sending_paused") == "bounce rate 6% over 5% limit"


@pytest.mark.asyncio
async def test_a_non_json_answer_is_an_error(state):
    def handler(request):
        return httpx.Response(200, text="<html>login</html>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CabinError):
            await sync(state, _cfg(), ENV, client=client)
