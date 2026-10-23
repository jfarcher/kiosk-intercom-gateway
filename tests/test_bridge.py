"""SIP → kiosk calls end to end, against a fake Asterisk and a fake kiosk."""

from __future__ import annotations

import asyncio
import json
import struct

import pytest

from fakes import FakeAri, FakeKiosk, ints, le_samples
from kig.ari import Ari
from kig.bridge import CallManager
from kig.probe import HttpProber
from kig.protocol import FRAME_BYTES, issue_token
from kig.roster import Roster
from kig.rtp import CHUNK_BYTES, parse_rtp, swap16
from kig.state import State

KEY = "shared-intercom-key"


async def until(cond, timeout=3.0, what="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


class Rig:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    async def start(self, password="secret", ring_timeout=60.0):
        self.kiosk = FakeKiosk(KEY)
        await self.kiosk.start()
        self.fake_ari = FakeAri()
        await self.fake_ari.start()
        self.prober = HttpProber()
        self.roster = Roster(self.prober, key=KEY)
        self.roster.heard({"id": self.kiosk.id, "name": "Kitchen", "address": "127.0.0.1",
                           "port": self.kiosk.port})
        await self.roster.probe_due()
        self.ari = Ari(self.fake_ari.url, "kig", password, "kiosk-intercom")
        self.calls = CallManager(self.roster, State(self.tmp_path / "state.json"), self.ari,
                                 listen_host="127.0.0.1", ring_timeout=ring_timeout)
        await self.calls.start()
        self.stop = asyncio.Event()
        self.ari_task = asyncio.ensure_future(self.ari.run(self.calls.on_event, self.stop))
        if password == "secret":
            await asyncio.wait_for(self.fake_ari.event_ws_connected.wait(), 3)
        return self

    async def close(self):
        self.stop.set()
        await asyncio.wait_for(self.ari_task, 3)
        await self.calls.stop()
        await self.ari.close()
        await self.prober.close()
        await self.kiosk.close()
        await self.fake_ari.close()

    async def ringing(self, cid="sip-1", target="801"):
        await self.fake_ari.incoming(cid, target)
        await until(lambda: self.kiosk.calls, what="the kiosk to ring")
        return self.kiosk.last_call()

    async def connected(self, cid="sip-1"):
        call = await self.ringing(cid)
        assert await self.kiosk.answer(call) == 200
        await asyncio.wait_for(self.kiosk.ws_open.wait(), 3)
        await until(lambda: self.calls.calls and next(iter(self.calls.calls.values())).state == "in_call",
                    what="in_call")
        return call


@pytest.fixture
async def rig(tmp_path):
    r = await Rig(tmp_path).start()
    yield r
    await r.close()


async def test_kiosk_gets_extension_801(rig):
    assert rig.roster.kiosks[rig.kiosk.id].extra["extension"] == "801"
    assert rig.roster.snapshot()[0]["extension"] == "801"


async def test_call_answer_audio_both_ways_and_kiosk_hangs_up(rig):
    call = await rig.ringing()
    c = rig.kiosk.calls[call]
    assert c["kind"] == "call" and c["from"]["name"] == "Alice (101)"
    assert c["port"] == rig.calls.port and c["from"]["id"].startswith("kig-")
    assert rig.fake_ari.calls_to("POST", "/channels/sip-1/ring")

    assert await rig.kiosk.answer(call) == 200
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)
    await until(lambda: rig.calls.calls[call].state == "in_call", what="in_call")
    assert rig.fake_ari.calls_to("POST", "/channels/sip-1/answer")
    media = rig.fake_ari.calls_to("POST", "/channels/externalMedia")[0]
    assert media["format"] == "slin16" and media["encapsulation"] == "rtp"
    assert media["external_host"].startswith("127.0.0.1:")
    assert rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")[0]["channel"] == "sip-1,ext-1"

    # Asterisk → kiosk: 8 × 20 ms big-endian packets become 2 × 80 ms LE frames.
    peer, target = rig.fake_ari.rtp["ext-1"]
    sent = le_samples(0, 320 * 8)
    for i in range(8):
        peer.send_be(target, sent[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES], seq=i)
    await until(lambda: len(rig.kiosk.frames) >= 2, what="frames at the kiosk")
    assert all(len(f) == FRAME_BYTES for f in rig.kiosk.frames)
    assert ints(b"".join(rig.kiosk.frames[:2])) == ints(sent)
    # A loud ramp counts as talking.
    await until(lambda: any('"on":true' in t for t in rig.kiosk.texts), what="talk on")

    # kiosk → Asterisk: 2 × 80 ms LE frames become 8 paced 20 ms BE packets.
    spoken = le_samples(5000, 320 * 8)
    await rig.kiosk.talk(spoken[:FRAME_BYTES])
    await rig.kiosk.talk(spoken[FRAME_BYTES:])
    await until(lambda: len(peer.packets) >= 8, what="RTP at Asterisk")
    parsed = [parse_rtp(p) for p in peer.packets[:8]]
    assert all(pt == 118 and len(pl) == CHUNK_BYTES for pt, pl in parsed)
    assert ints(swap16(b"".join(pl for _, pl in parsed))) == ints(spoken)
    seqs = [struct.unpack_from("!H", p, 2)[0] for p in peer.packets[:8]]
    stamps = [struct.unpack_from("!I", p, 4)[0] for p in peer.packets[:8]]
    assert all((b - a) % 65536 == 1 for a, b in zip(seqs, seqs[1:]))
    assert all((b - a) % 2**32 == 320 for a, b in zip(stamps, stamps[1:]))
    assert peer.packets[0][1] & 0x80  # marker on the first packet of a talk spurt

    await rig.kiosk.hang_up()
    await until(lambda: not rig.calls.calls, what="the call to end")
    assert rig.fake_ari.hangups("sip-1") == ["normal"]
    assert rig.fake_ari.hangups("ext-1")
    assert rig.fake_ari.calls_to("DELETE", "/bridges/bridge-1")
    assert rig.calls.history[-1]["reason"] == "ended"


async def test_caller_hangs_up_while_ringing_cancels(rig):
    call = await rig.ringing()
    rig.fake_ari.caller_hangs_up("sip-1")
    await until(lambda: (call, "cancel") in rig.kiosk.signals, what="cancel")
    await until(lambda: not rig.calls.calls)
    assert rig.fake_ari.hangups("sip-1") == []  # it was already gone


async def test_caller_hangs_up_during_call_ends_kiosk_socket(rig):
    await rig.connected()
    rig.fake_ari.caller_hangs_up("sip-1")
    await asyncio.wait_for(rig.kiosk.ws_closed.wait(), 3)
    assert any(json.loads(t).get("type") == "end" for t in rig.kiosk.texts)
    await until(lambda: not rig.calls.calls)


async def test_kiosk_declines(rig):
    await rig.ringing()
    assert await rig.kiosk.decline() == 200
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="hangup")
    assert rig.fake_ari.hangups("sip-1") == ["busy"]


@pytest.mark.parametrize("mode,cause", [("busy", "busy"), ("dnd", "busy"), ("off", "congestion")])
async def test_kiosk_refuses(rig, mode, cause):
    rig.kiosk.mode = mode
    await rig.fake_ari.incoming("sip-1", "801")
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="hangup")
    assert rig.fake_ari.hangups("sip-1") == [cause]


async def test_dnd_kiosk_is_refused_without_ringing(rig):
    rig.roster.kiosks[rig.kiosk.id].dnd = True
    await rig.fake_ari.incoming("sip-1", "801")
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="hangup")
    assert rig.fake_ari.hangups("sip-1") == ["busy"] and not rig.kiosk.calls


async def test_unknown_extension(rig):
    await rig.fake_ari.incoming("sip-1", "899")
    await until(lambda: rig.fake_ari.hangups("sip-1"), what="hangup")
    assert rig.fake_ari.hangups("sip-1") == ["unallocated"]


async def test_dial_by_name(rig):
    call = await rig.ringing(target="kitchen")
    assert call in rig.kiosk.calls


async def test_ring_timeout(tmp_path):
    r = await Rig(tmp_path).start(ring_timeout=0.3)
    try:
        call = await r.ringing()
        await until(lambda: (call, "cancel") in r.kiosk.signals, what="cancel")
        await until(lambda: r.fake_ari.hangups("sip-1"), what="hangup")
        assert r.fake_ari.hangups("sip-1") == ["no_answer"]
    finally:
        await r.close()


async def test_signal_needs_valid_token(rig):
    import aiohttp

    call = await rig.ringing()
    url = f"http://127.0.0.1:{rig.calls.port}/api/intercom/call/{call}"
    async with aiohttp.ClientSession() as s:
        bad = issue_token("other-key", call, "x")
        async with s.post(url, json={"action": "answer"}, headers={"Authorization": f"Bearer {bad}"}) as r:
            assert r.status == 403
        wrong_call = issue_token(KEY, "another-call", "x")
        async with s.post(url, json={"action": "answer"},
                          headers={"Authorization": f"Bearer {wrong_call}"}) as r:
            assert r.status == 403
    assert rig.calls.calls[call].state == "ringing"


async def test_gateway_identity(rig):
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{rig.calls.port}/api/intercom/identity") as r:
            data = await r.json()
    assert data["enabled"] and data["endpoint"]["port"] == rig.calls.port
    assert data["key"] == rig.roster.key_fingerprint


async def test_wrong_ari_password(tmp_path):
    r = await Rig(tmp_path).start(password="nope")
    try:
        await until(lambda: r.ari.error, what="an error")
        assert not r.ari.connected and "password" in r.ari.error
    finally:
        await r.close()


def test_extensions_are_stable(tmp_path):
    s = State(tmp_path / "s.json")
    assert s.extension_for("a", 801) == "801"
    assert s.extension_for("b", 801) == "802"
    again = State(tmp_path / "s.json")
    assert again.gateway_id == s.gateway_id
    assert again.extension_for("b", 801) == "802" and again.kiosk_for("801") == "a"
