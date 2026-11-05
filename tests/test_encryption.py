"""Kiosks with Encrypt communications on: HTTPS/WSS only, both ways."""

from __future__ import annotations

import asyncio
import json
import ssl
from types import SimpleNamespace

import aiohttp
import pytest

from fakes import ints, le_samples
from kig.media import CHUNK_BYTES
from kig.phones import PhoneDirectory
from kig.protocol import FRAME_BYTES
from kig.tls import Certificate, client_context
from test_bridge import Rig, until


ALICE = {"technology": "PJSIP", "resource": "101", "state": "online", "channel_ids": []}


@pytest.fixture
async def rig(tmp_path):
    r = await Rig(tmp_path).start(encrypted=True)
    r.fake_ari.endpoints = [dict(ALICE)]
    r.fake_ari.callerids = {"101": '"Alice" <101>'}
    c = r.calls
    r.phones = PhoneDirectory(
        r.ari, r.roster, c.state, None,
        {"identity": c.phone_identity, "call": c.phone_call, "signal": c.phone_signal,
         "audio": c.phone_audio},
        listen_host="127.0.0.1", certificate=Certificate(tmp_path),
    )
    c.phones = r.phones
    await until(lambda: r.ari.connected, what="ARI")
    await r.phones.refresh()
    yield r
    await r.phones.close()
    await r.close()


async def get_json(url: str) -> dict:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=client_context())) as s:
        async with s.get(url) as r:
            return await r.json()


async def test_an_encrypted_kiosk_is_ready(rig):
    k = rig.roster.kiosks[rig.kiosk.id]
    assert k.encrypted and k.intercom_url.startswith("https://")
    assert k.to_json(rig.roster.key_fingerprint)["status"] == "ready"
    assert rig.calls.tls_port and rig.calls.tls_port != rig.calls.port


async def test_phone_calls_an_encrypted_kiosk(rig):
    await rig.fake_ari.incoming("sip-1", "801", name="Alice", number="101")
    await until(lambda: rig.kiosk.calls, what="the kiosk to ring")  # it took the call over HTTPS
    call = rig.kiosk.last_call()
    frm = rig.kiosk.calls[call]["from"]
    assert frm["tls"] is True and frm["port"] == rig.calls.tls_port  # it answers us over TLS

    assert await rig.kiosk.answer(call) == 200
    await asyncio.wait_for(rig.kiosk.ws_open.wait(), 3)  # WSS, or the kiosk would refuse it (426)
    ext = next(iter(rig.fake_ari.media))
    voice = le_samples(0, 320 * 8)
    for i in range(8):
        rig.fake_ari.media[ext].send(voice[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES])
    await until(lambda: len(rig.kiosk.frames) >= 2, what="phone audio at the kiosk")
    assert ints(b"".join(rig.kiosk.frames[:2])) == ints(voice)

    rig.fake_ari.caller_hangs_up("sip-1")
    await asyncio.wait_for(rig.kiosk.ws_closed.wait(), 3)
    await until(lambda: not rig.calls.calls, what="clean up")


async def test_an_encrypted_kiosk_calls_a_phone(rig):
    alice = rig.phones.phones["101"]
    assert alice.tls_port and alice.tls_port != alice.port
    # The kiosk reads the phone's identity (over plain HTTP, as discovery may)
    # and is pointed at its encrypted port.
    ident = await get_json(f"http://127.0.0.1:{alice.port}/api/intercom/identity")
    assert ident["endpoint"] == {"port": alice.tls_port, "tls": True}

    status, body = await rig.kiosk.place_call(alice.tls_port, tls=True)
    assert (status, body) == (200, {"status": "ringing"})
    await until(lambda: rig.fake_ari.originated, what="Asterisk to dial")
    o = rig.fake_ari.originated[-1]
    assert rig.calls.calls["kiosk-call-1"].peer_url.startswith("https://")

    rig.fake_ari.phone_answers(o["id"])
    await until(lambda: ("kiosk-call-1", "answer") in rig.kiosk.signals, what="answer, over HTTPS")
    await asyncio.wait_for(rig.kiosk.out_open.wait(), 3)
    await until(lambda: rig.calls.calls["kiosk-call-1"].state == "in_call", what="in_call")
    said = le_samples(9000, 320 * 4)
    await rig.kiosk.out_talk(said[:FRAME_BYTES])
    ext = rig.fake_ari.calls_to("POST", "/bridges/bridge-1/addChannel")[0]["channel"].split(",")[1]
    sock = rig.fake_ari.media[ext]
    await until(lambda: len(sock.voice()) >= 4, what="kiosk audio at the phone")
    assert ints(b"".join(sock.voice()[:4])) == ints(said[: 4 * CHUNK_BYTES])

    rig.fake_ari.caller_hangs_up(o["id"])
    await asyncio.wait_for(rig.kiosk.out_closed.wait(), 3)
    assert any(json.loads(t).get("type") == "end" for t in rig.kiosk.out_texts)


async def test_encrypted_call_data_never_in_the_clear(rig):
    alice = rig.phones.phones["101"]
    rig.kiosk.encrypted = False  # an encrypted kiosk's call, sent to the plain port
    status, body = await rig.kiosk.place_call(alice.port, tls=True)
    assert status == 409 and body["status"] == "tls"
    assert not rig.fake_ari.originated


async def test_plain_and_encrypted_kiosks_together(rig):
    """Each kiosk is offered what it is: told apart by address."""
    plain = SimpleNamespace(secure=False, remote="10.0.0.9")
    rig.roster.heard({"id": "hall-id", "name": "Hall", "address": "10.0.0.9", "port": 2324})
    rig.roster.kiosks["hall-id"].enabled = True
    assert rig.calls.endpoint_for(plain, 1000, 2000) == {"port": 1000, "tls": False}
    mine = SimpleNamespace(secure=False, remote="127.0.0.1")
    assert rig.calls.endpoint_for(mine, 1000, 2000) == {"port": 2000, "tls": True}
    over_tls = SimpleNamespace(secure=True, remote="10.9.9.9")
    assert rig.calls.endpoint_for(over_tls, 1000, 2000)["tls"] is True
    # The gateway's "from" follows the kiosk it calls.
    hall = rig.roster.kiosks["hall-id"]
    assert rig.calls._self_info(hall, "Alice")["port"] == rig.calls.port
    assert "tls" not in rig.calls._self_info(hall, "Alice")


async def test_plain_kiosk_calling_a_phone_is_unchanged(tmp_path):
    r = await Rig(tmp_path).start()
    try:
        r.fake_ari.endpoints = [dict(ALICE)]
        c = r.calls
        r.phones = PhoneDirectory(
            r.ari, r.roster, c.state, None,
            {"identity": c.phone_identity, "call": c.phone_call, "signal": c.phone_signal,
             "audio": c.phone_audio},
            listen_host="127.0.0.1", certificate=Certificate(tmp_path),
        )
        c.phones = r.phones
        await until(lambda: r.ari.connected)
        await r.phones.refresh()
        alice = r.phones.phones["101"]
        ident = await get_json(f"http://127.0.0.1:{alice.port}/api/intercom/identity")
        assert ident["endpoint"] == {"port": alice.port, "tls": False}
        assert await r.kiosk.place_call(alice.port) == (200, {"status": "ringing"})
        await r.phones.close()
    finally:
        await r.close()


def test_certificate_is_made_once_and_kept(tmp_path):
    first = Certificate(tmp_path)
    first.server_context()
    pem = first.cert.read_bytes()
    assert oct(first.key.stat().st_mode & 0o777) == "0o600"
    Certificate(tmp_path).server_context()
    assert first.cert.read_bytes() == pem  # kept across restarts
    first.cert.write_text("damaged")
    assert isinstance(Certificate(tmp_path).server_context(), ssl.SSLContext)
    assert first.cert.read_bytes() != pem
