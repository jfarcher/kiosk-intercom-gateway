"""Home Assistant controls: call events, the call and hangup actions, sensors."""

from __future__ import annotations

import asyncio

import pytest

from kig.commands import handle, parse
from test_bridge import until
from test_phones import alice, ring_alice, rig  # noqa: F401  (fixture)


@pytest.fixture
def events(rig):  # noqa: F811
    got = []
    rig.calls.listeners.append(got.append)
    return got


async def test_events_for_a_phone_calling_a_kiosk(rig, events):  # noqa: F811
    await rig.fake_ari.incoming("sip-1", "801", name="Alice", number="101")
    await until(lambda: rig.kiosk.calls, what="the kiosk to ring")
    await rig.kiosk.answer()
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)
    await until(lambda: any(e["state"] == "answered" for e in events), what="answered")
    await rig.kiosk.hang_up()
    await until(lambda: any(e["state"] == "ended" for e in events), what="ended")
    assert [e["state"] for e in events] == ["ringing", "answered", "ended"]
    e = events[0]
    assert e["direction"] == "phone_to_kiosk" and e["kiosk"] == "Kitchen" and e["kiosk_extension"] == "801"
    assert e["phone"] == "Alice (101)" and e["phone_number"] == "101" and e["call_id"]
    assert events[-1]["reason"] == "ended" and events[-1]["duration"] >= 0


async def test_events_for_a_kiosk_calling_a_phone(rig, events):  # noqa: F811
    o = await ring_alice(rig)
    rig.fake_ari.phone_refuses(o["id"], 17)
    await until(lambda: any(e["state"] == "ended" for e in events), what="ended")
    assert [e["state"] for e in events] == ["ringing", "ended"]
    assert events[0]["direction"] == "kiosk_to_phone" and events[0]["phone_number"] == "101"
    assert events[-1]["reason"] == "busy" and events[-1]["duration"] == 0


async def test_event_for_a_call_to_no_kiosk(rig, events):  # noqa: F811
    await rig.fake_ari.incoming("sip-1", "899")
    await until(lambda: events, what="an event")
    assert events[0]["state"] == "ended" and events[0]["reason"] == "unknown_kiosk"


async def test_call_action_rings_the_phone_then_the_kiosk(rig, events):  # noqa: F811
    assert await handle(rig.calls, {"action": "call", "phone": "101", "kiosk": "kitchen"}) == ""
    o = rig.fake_ari.originated[-1]
    assert o["endpoint"] == "PJSIP/101" and o["appArgs"] == "801,Alice (101),101"
    assert o["callerId"] == '"Kitchen" <801>'  # the phone sees the kiosk calling
    assert not rig.kiosk.calls  # nothing at the kiosk until the phone answers

    rig.fake_ari.phone_answers(o["id"])
    await until(lambda: rig.kiosk.calls, what="the kiosk to ring")
    frm = rig.kiosk.calls[rig.kiosk.last_call()]["from"]
    assert frm["name"] == "Alice (101)" and frm["id"] == alice(rig).id  # shows the phone, can call back
    plays = rig.fake_ari.calls_to("POST", "/bridges/bridge-1/play")
    assert plays and plays[0]["media"] == "tone:ring"  # the phone hears ringback meanwhile, in its bridge
    assert rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")[0]["channel"] == o["id"]
    assert not rig.fake_ari.calls_to("POST", f"/channels/{o['id']}/play")  # never on the channel

    await rig.kiosk.answer()
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)
    await until(lambda: rig.fake_ari.calls_to("DELETE", "/playbacks/"), what="ringback stopped")
    await until(lambda: len(rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")) == 2,
                what="the kiosk's audio joining the same bridge")
    assert rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")[1]["channel"].startswith("ext-")
    created = [q for q in rig.fake_ari.calls_to("POST", "/bridges") if q.get("type") == "mixing"]
    assert len(created) == 1  # one bridge, not a second


@pytest.mark.parametrize("command,error", [
    ({"action": "call", "phone": "101", "kiosk": "garage"}, "no kiosk"),
    ({"action": "call", "phone": "101"}, "needs phone and kiosk"),
    ({"action": "dance"}, "unknown action"),
])
async def test_call_action_errors(rig, command, error):  # noqa: F811
    assert error in await handle(rig.calls, command)
    assert not rig.fake_ari.originated


async def test_hangup_action(rig):  # noqa: F811
    await ring_alice(rig, "a")
    assert await rig.calls.action_hangup(kiosk="nowhere") == 0
    assert await rig.calls.action_hangup(phone="999") == 0
    assert await handle(rig.calls, {"action": "hangup", "phone": "101"}) == ""
    await until(lambda: not rig.calls.calls)
    await until(lambda: ("a", "decline") in rig.kiosk.signals or ("a", "hangup") in rig.kiosk.signals
                or ("a", "missed") in rig.kiosk.signals, what="the kiosk told")
    await ring_alice(rig, "b")
    assert await rig.calls.action_hangup() == 1  # all


def test_parse():
    assert parse('{"action": "call", "phone": "101", "kiosk": "Kitchen"}')["phone"] == "101"
    assert parse('"{\\"action\\": \\"hangup\\"}"') == {"action": "hangup"}  # input given as a string
    assert parse("hangup") is None and parse("[1]") is None
