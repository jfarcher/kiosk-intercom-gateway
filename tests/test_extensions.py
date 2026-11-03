"""Kiosk extensions: a reinstalled kiosk (same address, new intercom id) keeps
its number; what the page shows is what a dialled number reaches; numbers can
be set by hand to recover."""

from __future__ import annotations

import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kig.bridge import CallManager
from kig.protocol import fingerprint
from kig.roster import Roster
from kig.state import State
from kig.web import make_app

KEY = "k"
OLD, NEW = "1bc277a930a66a0b", "f0c229c418511584"
ISABELLE = {"name": "Isabelle Echo Show", "address": "192.168.6.189", "port": 2324}
URL = "http://192.168.6.189:2324/api/intercom/identity"


def identity(kid: str, name: str = "Isabelle Echo Show") -> dict:
    return {"id": kid, "name": name, "version": "2026.11.1", "enabled": True, "key": fingerprint(KEY),
            "dnd": False, "endpoint": {"port": 2325, "tls": False}}


class Prober:
    def __init__(self):
        self.answers: dict[str, tuple[int, dict]] = {}

    async def __call__(self, url):
        return self.answers.get(url)


def setup(tmp_path, extensions: dict, addresses: dict | None = None):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"gateway_id": "kig-test", "extensions": extensions,
                                "addresses": addresses or {}}))
    prober = Prober()
    roster = Roster(prober, key=KEY)
    state = State(path)
    calls = CallManager(roster, state, None)
    return prober, roster, state, calls


def shown(roster, kid):
    return next(k for k in roster.snapshot() if k["id"] == kid)["extension"]


def saved(state):
    return json.loads(state.path.read_text())["extensions"]


async def known_old_kiosk(tmp_path, **kw):
    prober, roster, state, calls = setup(tmp_path, {OLD: "803", "bedroom-id": "801"}, **kw)
    roster.heard({"id": OLD, **ISABELLE})
    prober.answers[URL] = (200, identity(OLD))
    await roster.probe_due()
    assert shown(roster, OLD) == "803" and calls.kiosk_for("803").id == OLD
    return prober, roster, state, calls


def assert_kept_803(roster, state, calls):
    k = calls.kiosk_for("803")
    assert k is not None and k.id == NEW, "803 must reach the reinstalled kiosk"
    assert list(roster.kiosks) == [NEW], "one kiosk object, under the new id"
    assert shown(roster, NEW) == "803"
    assert state.extensions == {NEW: "803", "bedroom-id": "801"}  # old id gone, no stray number for NEW
    assert saved(state) == state.extensions


async def test_reinstall_announced_then_probed_keeps_extension(tmp_path, caplog):
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    caplog.set_level(logging.INFO)
    roster.heard({"id": NEW, **ISABELLE})  # back under a new id, same address
    prober.answers[URL] = (200, identity(NEW))
    for k in roster.kiosks.values():
        k.probed_at = None
    await roster.probe_due()
    assert_kept_803(roster, state, calls)
    assert f"changed id {OLD} → {NEW} (reinstalled); keeps extension 803" in caplog.text


async def test_reinstall_probed_before_announced_keeps_extension(tmp_path):
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    prober.answers[URL] = (200, identity(NEW))
    for k in roster.kiosks.values():
        k.probed_at = None
    await roster.probe_due()  # the probe sees the new id first
    roster.heard({"id": NEW, **ISABELLE})
    assert_kept_803(roster, state, calls)


async def test_number_given_to_the_new_id_meanwhile_is_freed(tmp_path):
    """State from 0.3.1: the new id already has a number of its own (804)."""
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    state.extensions[NEW] = "804"
    prober.answers[URL] = (200, identity(NEW))
    for k in roster.kiosks.values():
        k.probed_at = None
    await roster.probe_due()
    assert_kept_803(roster, state, calls)
    assert state.kiosk_for("804") is None


async def test_reinstall_while_the_gateway_was_off(tmp_path):
    """Restart: no old object in memory, but State remembers where the old id was."""
    prober, roster, state, calls = setup(tmp_path, {OLD: "803"}, addresses={OLD: "192.168.6.189:2324"})
    roster.heard({"id": NEW, **ISABELLE})
    assert calls.kiosk_for("803").id == NEW and shown(roster, NEW) == "803"
    assert state.extensions == {NEW: "803"} and state.addresses == {NEW: "192.168.6.189:2324"}


async def test_another_kiosk_elsewhere_gets_its_own_number(tmp_path):
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    roster.heard({"id": "garage-id", "name": "Garage", "address": "192.168.6.50", "port": 2324})
    assert shown(roster, "garage-id") == "802"  # the lowest free one
    assert calls.kiosk_for("803").id == OLD and state.extensions[OLD] == "803"


async def test_reused_address_does_not_take_a_moved_kiosks_number(tmp_path):
    """DHCP: the old kiosk moved to another address; a different device got its IP."""
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    roster.heard({"id": OLD, **ISABELLE, "address": "192.168.6.77"})  # moved
    roster.heard({"id": "stranger", "name": "Tablet", "address": "192.168.6.189", "port": 2324})
    assert calls.kiosk_for("803").id == OLD and shown(roster, "stranger") == "802"


async def test_shown_number_is_always_the_dialled_one(tmp_path):
    prober, roster, state, calls = await known_old_kiosk(tmp_path)
    for k in roster.snapshot():
        assert calls.kiosk_for(k["extension"]).id == k["id"]


# ── recovery: setting a number by hand ──────────────────────────────


async def broken_031_state(tmp_path):
    """What 0.3.1 left behind: the reinstalled kiosk on a new number (806); 803
    still in State for the old id, which is no longer in the roster."""
    prober, roster, state, calls = setup(tmp_path, {OLD: "803", NEW: "806", "bedroom-id": "801"})
    roster.heard({"id": NEW, **ISABELLE})
    roster.heard({"id": "bedroom-id", "name": "Bedroom", "address": "192.168.6.20", "port": 2324})
    assert shown(roster, NEW) == "806" and calls.kiosk_for("803") is None
    app = make_app(roster, lambda: {}, set_extension=calls.set_extension)
    return roster, state, calls, app


async def test_set_extension_takes_a_number_from_an_absent_id(tmp_path):
    roster, state, calls, app = await broken_031_state(tmp_path)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/api/extension", json={"kiosk": NEW, "extension": "803"})
        assert r.status == 200 and (await r.json())["extension"] == "803"
    assert calls.kiosk_for("803").id == NEW and shown(roster, NEW) == "803"
    assert state.extensions == {NEW: "803", "bedroom-id": "801"}  # 806 free, old id gone
    assert saved(state) == state.extensions


async def test_set_extension_refuses_a_present_kiosks_number(tmp_path):
    roster, state, calls, app = await broken_031_state(tmp_path)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/api/extension", json={"kiosk": NEW, "extension": "801"})
        body = await r.json()
    assert r.status == 409 and "Bedroom" in body["error"]
    assert shown(roster, NEW) == "806" and calls.kiosk_for("801").id == "bedroom-id"


@pytest.mark.parametrize("body,status", [
    ({"kiosk": NEW, "extension": "8a3"}, 400),
    ({"kiosk": NEW, "extension": "8"}, 400),
    ({"kiosk": "nobody", "extension": "805"}, 400),
    ([], 400),
])
async def test_set_extension_bad_requests(tmp_path, body, status):
    roster, state, calls, app = await broken_031_state(tmp_path)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/extension", json=body)).status == status
    assert shown(roster, NEW) == "806"


async def test_set_extension_by_name_and_outside_the_dialplan_pattern(tmp_path):
    roster, state, calls, app = await broken_031_state(tmp_path)
    result = calls.set_extension("isabelle echo show", "1803")
    assert result["ok"] and "dialplan" in result["warning"] and shown(roster, NEW) == "1803"
