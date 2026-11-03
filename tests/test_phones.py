"""Kiosk → SIP phone calls, against a fake Asterisk and a fake kiosk that calls
the way Kiosk Satellite does."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from fakes import ints, le_samples
from kig.media import CHUNK_BYTES
from kig.phones import Phone, PhoneDirectory, parse_phones
from kig.protocol import FRAME_BYTES, fingerprint
from test_bridge import KEY, Rig, until

PHONES = [
    {"technology": "PJSIP", "resource": "101", "state": "online", "channel_ids": []},
    {"technology": "PJSIP", "resource": "102", "state": "offline", "channel_ids": []},
    {"technology": "PJSIP", "resource": "provider-trunk", "state": "online", "channel_ids": []},
]


@pytest.fixture
async def rig(tmp_path):
    r = await Rig(tmp_path).start()
    r.fake_ari.endpoints = [dict(e) for e in PHONES]
    r.fake_ari.callerids = {"101": '"Alice" <101>'}
    calls = r.calls
    r.phones = PhoneDirectory(
        r.ari, r.roster, calls.state, None,
        {"identity": calls.phone_identity, "call": calls.phone_call,
         "signal": calls.phone_signal, "audio": calls.phone_audio},
        listen_host="127.0.0.1",
    )
    calls.phones = r.phones
    await until(lambda: r.ari.connected, what="ARI")
    await r.phones.refresh()
    yield r
    await r.phones.close()
    await r.close()


def alice(rig) -> Phone:
    return rig.phones.phones["101"]


async def ring_alice(rig, call="kiosk-call-1"):
    status, body = await rig.kiosk.place_call(alice(rig).port, call)
    assert (status, body) == (200, {"status": "ringing"})
    await until(lambda: rig.fake_ari.originated, what="Asterisk to dial")
    return rig.fake_ari.originated[-1]


def test_parse_phones():
    assert parse_phones(["101", "102=Front door", " 103 : Office ", "", "104="]) == {
        "101": "", "102": "Front door", "103": "Office", "104": ""}


async def test_offers_registered_numbered_phones(rig):
    assert set(rig.phones.phones) == {"101", "102"}  # the trunk is not a phone
    assert alice(rig).label == "Alice (101)" and alice(rig).runner is not None
    assert rig.phones.phones["102"].runner is None  # offline: not offered
    assert rig.phones.phones["102"].name == "Phone 102"  # no caller ID configured
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{alice(rig).port}/api/intercom/identity") as r:
            ident = await r.json()
    assert ident == {"id": f"{rig.calls.state.gateway_id}-101", "name": "Alice (101)",
                     "version": ident["version"], "enabled": True, "key": fingerprint(KEY), "dnd": False,
                     "endpoint": {"port": alice(rig).port, "tls": False}}
    assert rig.calls.state.phone_ports["101"] == alice(rig).port


async def test_phone_comes_and_goes_with_registration(rig):
    port = alice(rig).port
    rig.fake_ari.endpoints[0]["state"] = "offline"
    await rig.phones.refresh()
    assert alice(rig).runner is None
    rig.fake_ari.endpoints[0]["state"] = "online"
    await rig.phones.refresh()
    assert alice(rig).runner is not None and alice(rig).port == port  # same port again


async def test_configured_phone_list_and_names(rig):
    rig.phones.configured = parse_phones(["provider-trunk=Reception"])
    rig.phones.phones.clear()
    await rig.phones.refresh()
    assert list(rig.phones.phones) == ["provider-trunk"]
    assert rig.phones.phones["provider-trunk"].label == "Reception (provider-trunk)"


async def test_kiosk_calls_phone_answer_audio_and_phone_hangs_up(rig):
    o = await ring_alice(rig)
    assert o["endpoint"] == "PJSIP/101" and o["app"] == "kiosk-intercom"
    assert o["appArgs"] == "kig-out,kiosk-call-1"
    assert o["callerId"] == '"Kitchen" <801>'  # the phone can call the kiosk back
    assert int(o["timeout"]) <= 30

    rig.fake_ari.phone_answers(o["id"])
    await until(lambda: ("kiosk-call-1", "answer") in rig.kiosk.signals, what="answer at the kiosk")
    await asyncio.wait_for(rig.kiosk.out_open.wait(), 3)
    await until(lambda: rig.calls.calls["kiosk-call-1"].state == "in_call", what="in_call")
    media = rig.fake_ari.calls_to("POST", "/channels/externalMedia")[0]
    assert media["encapsulation"] == "audiosocket" and media["format"] == "slin16"
    add = rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")[0]
    sip, ext = add["channel"].split(",")
    assert sip == o["id"] and ext in rig.fake_ari.media

    sock = rig.fake_ari.media[ext]
    heard = le_samples(0, 320 * 8)
    for i in range(8):
        sock.send(heard[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES])
    await until(lambda: len(rig.kiosk.out_frames) >= 2, what="phone audio at the kiosk")
    assert ints(b"".join(rig.kiosk.out_frames[:2])) == ints(heard)

    said = le_samples(9000, 320 * 4)
    await rig.kiosk.out_talk(said[:FRAME_BYTES])
    await until(lambda: len(sock.audio()) >= 4, what="kiosk audio at the phone")
    assert ints(b"".join(sock.audio()[:4])) == ints(said[: 4 * CHUNK_BYTES])

    rig.fake_ari.caller_hangs_up(o["id"])
    await asyncio.wait_for(rig.kiosk.out_closed.wait(), 3)
    assert any(json.loads(t).get("type") == "end" for t in rig.kiosk.out_texts)
    await until(lambda: not rig.calls.calls, what="clean up")
    assert rig.fake_ari.hangups(ext) and rig.fake_ari.calls_to("DELETE", "/bridges/bridge-1")


async def test_kiosk_hangs_up_during_call(rig):
    o = await ring_alice(rig)
    rig.fake_ari.phone_answers(o["id"])
    await asyncio.wait_for(rig.kiosk.out_open.wait(), 3)
    await until(lambda: rig.calls.calls["kiosk-call-1"].state == "in_call")
    await rig.kiosk.out_hang_up()
    await until(lambda: rig.fake_ari.hangups(o["id"]), what="the phone hung up")
    assert rig.fake_ari.hangups(o["id"]) == ["normal"]
    await until(lambda: not rig.calls.calls)


async def test_kiosk_cancels_while_ringing(rig):
    o = await ring_alice(rig)
    assert await rig.kiosk.signal_callee("cancel") == 200
    await until(lambda: rig.fake_ari.hangups(o["id"]), what="the phone to stop ringing")
    await until(lambda: not rig.calls.calls)
    assert not any(a == "decline" for _, a in rig.kiosk.signals)  # the kiosk ended it; no reply needed


@pytest.mark.parametrize("cause,signal", [(17, "decline"), (21, "decline"), (19, "missed"), (20, "decline")])
async def test_phone_does_not_answer(rig, cause, signal):
    o = await ring_alice(rig)
    rig.fake_ari.phone_refuses(o["id"], cause)
    await until(lambda: ("kiosk-call-1", signal) in rig.kiosk.signals, what=signal)
    await until(lambda: not rig.calls.calls)


async def test_refusals(rig):
    port = alice(rig).port
    assert await rig.kiosk.place_call(port, "c-bad", key="other") == (403, {"status": "key"})
    assert await rig.kiosk.place_call(port, "c-tls", tls=True) == (409, {"status": "tls"})
    assert await rig.kiosk.place_call(port, "c-all", kind="broadcast") == (200, {"status": "refused"})
    alice(rig).busy = True
    assert await rig.kiosk.place_call(port, "c-busy") == (200, {"status": "busy"})
    assert not rig.fake_ari.originated


async def test_one_call_at_a_time_per_phone(rig):
    await ring_alice(rig, "first")
    assert await rig.kiosk.place_call(alice(rig).port, "second") == (200, {"status": "busy"})


async def test_audio_socket_needs_its_call(rig):
    async with aiohttp.ClientSession() as s:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await s.ws_connect(f"ws://127.0.0.1:{alice(rig).port}/api/intercom/audio/nope?token=x")


async def test_call_from_an_offered_phone_can_be_called_back(rig):
    await rig.fake_ari.incoming("sip-1", "801", name="Alice", number="101")
    await until(lambda: rig.kiosk.calls, what="the kiosk to ring")
    assert rig.kiosk.calls[rig.kiosk.last_call()]["from"]["id"] == alice(rig).id
    await rig.fake_ari.incoming("sip-2", "801", name="Door", number="555")
    await until(lambda: len(rig.kiosk.calls) == 2)
    assert rig.kiosk.calls[rig.kiosk.last_call()]["from"]["id"] == rig.calls.state.gateway_id


async def test_phone_page_explains_itself(rig):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{alice(rig).port}/") as r:
            assert r.status == 200 and "Alice (101)" in await r.text()


async def test_choosing_phones_live(rig):
    snap = {p["extension"]: p for p in rig.phones.snapshot()}
    assert set(snap) == {"101", "102", "provider-trunk"}  # every endpoint is listed
    assert snap["101"]["selected"] and snap["101"]["offered"] and snap["101"]["callerid"] == "Alice"
    assert not snap["provider-trunk"]["selected"] and snap["102"]["selected"] and not snap["102"]["offered"]
    assert rig.phones.auto

    chosen = await rig.phones.choose(["provider-trunk=Reception", "101=Front door"])
    assert chosen == ["provider-trunk=Reception", "101=Front door"] and not rig.phones.auto
    assert set(rig.phones.phones) == {"provider-trunk", "101"}  # 102 dropped
    assert alice(rig).label == "Front door (101)"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{alice(rig).port}/api/intercom/identity") as r:
            assert (await r.json())["name"] == "Front door (101)"

    assert await rig.phones.choose(None) == [] and rig.phones.auto
    assert set(rig.phones.phones) == {"101", "102"} and alice(rig).label == "Alice (101)"


async def test_phones_api():
    from aiohttp.test_utils import TestClient, TestServer

    from kig.roster import Roster
    from kig.web import make_app

    got = []

    async def set_phones(entries):
        got.append(entries)
        return {"ok": True, "saved": True}

    async def never(_):
        return None

    app = make_app(Roster(never), lambda: {}, set_phones=set_phones)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/api/phones", json={"phones": [{"extension": "101", "name": " Front  door "},
                                                              {"extension": "102", "name": ""}]})
        assert (await r.json())["ok"] and got[-1] == ["101=Front door", "102"]
        r = await client.post("/api/phones", json={"auto": True})
        assert (await r.json())["ok"] and got[-1] is None
        bad_bodies = (
            {"phones": [{"extension": "1=2"}]},
            {"phones": [{"extension": "101", "name": "a=b"}]},
            [],
        )
        for bad in bad_bodies:
            assert (await client.post("/api/phones", json=bad)).status == 400
    assert len(got) == 2


async def test_shows_which_kiosks_found_a_phone(rig):
    assert next(p for p in rig.phones.snapshot() if p["extension"] == "101")["seen_by"] == []
    # The kiosk at 127.0.0.1 checks the phone, as it does every ~30 s.
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{alice(rig).port}/api/intercom/identity") as r:
            assert r.status == 200
    assert next(p for p in rig.phones.snapshot() if p["extension"] == "101")["seen_by"] == ["Kitchen"]
