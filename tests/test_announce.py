"""Announce to all: a phone dials 800 and every Ready kiosk hears it, one way."""

from __future__ import annotations

import asyncio
import json

import pytest

from fakes import FakeKiosk, ints, le_samples
from kig.commands import handle
from kig.media import CHUNK_BYTES
from test_bridge import KEY, Rig, until


@pytest.fixture
async def rig(tmp_path):
    r = await Rig(tmp_path).start()
    r.bedroom = FakeKiosk(KEY, name="Bedroom", kid="bedroom-id")
    await r.bedroom.start()
    r.roster.heard({"id": "bedroom-id", "name": "Bedroom", "address": "127.0.0.1", "port": r.bedroom.port})
    await r.roster.probe_due()
    r.events = []
    r.calls.listeners.append(r.events.append)
    yield r
    await r.bedroom.close()
    await r.close()


async def announcing(rig, cid="sip-1"):
    await rig.fake_ari.incoming(cid, "800", name="Alice", number="101")
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)
    await asyncio.wait_for(rig.bedroom.ws_open.wait(), 3)
    await until(lambda: any(e["state"] == "answered" for e in rig.events), what="the announcement live")


async def test_every_ready_kiosk_hears_the_phone(rig):
    await announcing(rig)
    for k in (rig.kiosk, rig.bedroom):
        c = k.calls[k.last_call()]
        assert c["kind"] == "broadcast" and c["from"]["name"] == "Alice (101)"
    assert rig.fake_ari.calls_to("POST", "/channels/sip-1/answer")
    beep = rig.fake_ari.calls_to("POST", "/bridges/bridge-1/play")
    assert beep and beep[0]["media"] == "sound:beep"  # the cue to speak
    assert not rig.fake_ari.calls_to("POST", "/channels/sip-1/play")  # would block its hangup

    ext = next(c for c in rig.fake_ari.media)
    voice = le_samples(0, 320 * 8)
    for i in range(8):
        rig.fake_ari.media[ext].send(voice[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES])
    for k in (rig.kiosk, rig.bedroom):
        await until(lambda k=k: len(k.frames) >= 2, what=f"audio at {k.name}")
        assert ints(b"".join(k.frames[:2])) == ints(voice)

    # Asterisk gets a steady stream back (silence), or the bridge stalls.
    await until(lambda: len(rig.fake_ari.media[ext].audio()) >= 5, what="silence to Asterisk")
    assert set(rig.fake_ari.media[ext].audio()[-1]) == {0}

    live = rig.events[-1]
    assert live["direction"] == "phone_to_all" and sorted(live["kiosks"]) == ["Bedroom", "Kitchen"]
    assert live["kiosk_extension"] == "800" and live["phone_number"] == "101"

    rig.fake_ari.caller_hangs_up("sip-1")
    for k in (rig.kiosk, rig.bedroom):
        await asyncio.wait_for(k.ws_closed.wait(), 3)
        assert any(json.loads(t).get("type") == "end" for t in k.texts)
    await until(lambda: not rig.calls.announcements, what="clean up")
    assert rig.fake_ari.hangups(ext) and rig.fake_ari.calls_to("DELETE", "/bridges/bridge-1")
    assert rig.events[-1]["state"] == "ended" and rig.events[-1]["reason"] == "ended"


async def test_kiosks_that_decline_are_skipped(rig):
    rig.bedroom.accept_announcements = False
    await rig.fake_ari.incoming("sip-1", "800")
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)
    await until(lambda: any(e["state"] == "answered" for e in rig.events))
    assert rig.events[-1]["kiosks"] == ["Kitchen"] and not rig.bedroom.ws_open.is_set()


async def test_no_kiosk_takes_it(rig):
    rig.kiosk.accept_announcements = rig.bedroom.accept_announcements = False
    await rig.fake_ari.incoming("sip-1", "800")
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="the caller hung up")
    assert rig.fake_ari.hangups("sip-1") == ["congestion"]
    assert rig.events[-1]["state"] == "ended" and rig.events[-1]["reason"] == "unavailable"
    assert not rig.calls.announcements


async def test_everyone_dismisses_it(rig):
    await announcing(rig)
    await rig.kiosk.hang_up()
    await asyncio.sleep(0.2)
    assert rig.calls.announcements  # Bedroom still listens
    await rig.bedroom.hang_up()
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="the caller hung up")
    assert rig.fake_ari.hangups("sip-1") == ["normal"]


async def test_hangup_action_ends_it(rig):
    await announcing(rig)
    assert await handle(rig.calls, {"action": "hangup"}) == ""
    await until(lambda: not rig.calls.announcements)
    await asyncio.wait_for(rig.kiosk.ws_closed.wait(), 3)


async def test_call_action_to_all(rig):
    assert await handle(rig.calls, {"action": "call", "phone": "101", "kiosk": "all"}) == ""
    o = rig.fake_ari.originated[-1]
    assert o["appArgs"].startswith("800,") and o["callerId"] == '"All kiosks" <800>'


async def test_800_is_never_a_kiosks_number(rig):
    assert rig.calls.set_extension("kitchen-id", "800")["error"] == "800 is the Announce to all number."
    state = rig.calls.state
    assert state.extension_for("new-kiosk", 800, reserved={"800"}) not in ("800", "801")  # 801 is Kitchen's
