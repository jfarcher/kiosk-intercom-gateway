"""Real calls through a real Asterisk (the Home Assistant Asterisk app's image)
into a fake kiosk. Run by CI after the container is up:

    python3 tests/integration/e2e.py http://127.0.0.1:8088 kig ci-secret
    python3 tests/integration/e2e.py --recording kig-ci.wav

Call 1 (context kig-ci-tone): the phone plays 440 Hz; the kiosk must hear it.
Call 2 (context kig-ci-rec): the kiosk says 1000 Hz; Asterisk records it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
from array import array
from pathlib import Path

import aiohttp

sys.path[:0] = [str(Path(__file__).parents[1]), str(Path(__file__).parents[2] / "kiosk_intercom_gateway/src")]

from fakes import FakeKiosk, ints  # noqa: E402
from kig.ari import Ari  # noqa: E402
from kig.bridge import CallManager  # noqa: E402
from kig.commands import handle  # noqa: E402
from kig.phones import PhoneDirectory  # noqa: E402
from kig.probe import HttpProber  # noqa: E402
from kig.protocol import FRAME_BYTES, SAMPLE_RATE  # noqa: E402
from kig.roster import Roster  # noqa: E402
from kig.state import State  # noqa: E402
from kig.tls import Certificate  # noqa: E402

KEY = "ci-intercom-key"


def tone(freq: float, start: int, count: int, amp: int = 9000) -> bytes:
    return array("h", (int(amp * math.sin(2 * math.pi * freq * (start + i) / SAMPLE_RATE))
                       for i in range(count))).tobytes()


def frequency(samples: list[int], rate: int) -> float:
    crossings = sum(1 for a, b in zip(samples, samples[1:], strict=False) if a < 0 <= b)
    return crossings * rate / max(1, len(samples))


def level(samples: list[int]) -> float:
    return math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))


REPORT: list[str] = []
# The gateway's own log, attached to a failure.
LOG_LINES: list[str] = []


class _Keep(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_LINES.append(f"{record.relativeCreated / 1000:8.2f} {record.name}: {record.getMessage()}"[:300])
        del LOG_LINES[:-60]


_handler = _Keep()
logging.getLogger("kig").addHandler(_handler)
logging.getLogger("kig").setLevel(logging.DEBUG)


def flush_report() -> None:
    """All of it as one annotation: GitHub keeps only ten notices per step,
    and annotations are readable where the raw log is not."""
    if os.environ.get("GITHUB_ACTIONS") and REPORT:
        print("::notice title=e2e report::" + "%0A".join(REPORT), flush=True)
    REPORT.clear()


def note(text: str) -> None:
    print(text, flush=True)
    REPORT.append(text)


def check(ok: bool, what: str) -> None:
    line = ("PASS " if ok else "FAIL ") + what
    print(line, flush=True)
    REPORT.append(line)
    if not ok:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=e2e::{line}", flush=True)
        flush_report()
        if os.environ.get("GITHUB_ACTIONS"):  # the gateway's log, its own annotation
            print("::notice title=gateway log::" + "%0A".join(LOG_LINES[-45:]), flush=True)
        raise SystemExit(1)


async def wait(cond, seconds: float) -> bool:
    for _ in range(int(seconds * 10)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def one_call(s, url, kiosk: FakeKiosk, calls: CallManager, context: str, label: str,
                   ext: str = "801") -> None:
    kiosk.calls.clear()
    kiosk.frames.clear()
    kiosk.texts.clear()
    kiosk.ws_open.clear()
    kiosk.ws_closed.clear()
    async with s.post(f"{url}/ari/channels", params={
        "endpoint": f"Local/{ext}@default/n", "context": context, "extension": "s", "priority": "1",
        "callerId": "Doorbell <201>",
    }) as r:
        check(r.status == 200, f"{label}: Asterisk originated a call to {ext} (HTTP {r.status})")
    check(await wait(lambda: kiosk.calls, 10), f"{label}: the kiosk rang")
    note(f"{label}: caller shown on the kiosk: " + kiosk.calls[kiosk.last_call()]["from"]["name"])
    check(await kiosk.answer() == 200, f"{label}: the kiosk answered")
    await asyncio.wait_for(kiosk.ws_open.wait(), 10)
    check(True, f"{label}: the gateway opened the kiosk's audio socket")

    # The kiosk talks 1000 Hz in real time until the phone side hangs up.
    pos = 0
    call = next(iter(calls.calls.values()))
    for i in range(int(8000 / 80)):
        if kiosk.ws_closed.is_set():
            break
        await kiosk.talk(tone(1000, pos, FRAME_BYTES // 2))
        pos += FRAME_BYTES // 2
        await asyncio.sleep(0.08)
        if i == 5 and call.ext_channel and context == "kig-ci-rec":
            # Record what Asterisk receives from the gateway, straight off the
            # external media channel, independent of the far side's Record().
            async with s.post(f"{url}/ari/channels/{call.ext_channel}/snoop",
                              params={"spy": "in", "app": "kiosk-intercom"}) as r:
                snoop = await r.json() if r.status == 200 else None
                note(f"{label}: snoop HTTP {r.status}")
            if snoop:
                async with s.post(f"{url}/ari/channels/{snoop['id']}/record",
                                  params={"name": "kig-snoop", "format": "wav", "maxDurationSeconds": 4,
                                          "ifExists": "overwrite"}) as r:
                    note(f"{label}: snoop recording HTTP {r.status} {(await r.text())[:120]}")
    if call.media:
        note(f"{label}: gateway AudioSocket sent {call.media.sent}, received {call.media.received} "
             f"(20 ms messages) on port {calls.media_server.port}")

    check(await wait(lambda: kiosk.ws_closed.is_set(), 15), f"{label}: the call ended")
    check(any(json.loads(t).get("type") == "end" for t in kiosk.texts),
          f"{label}: the kiosk was told the caller hung up")
    check(all(len(x) == FRAME_BYTES for x in kiosk.frames), f"{label}: every frame to the kiosk is 80 ms")
    check(await wait(lambda: not calls.calls, 5), f"{label}: the gateway cleaned the call up")
    await asyncio.sleep(1)
    async with s.get(f"{url}/ari/channels") as r:
        left = await r.json()
    check(not left, f"{label}: no channels left in Asterisk {[c['name'] for c in left]}")
    async with s.get(f"{url}/ari/bridges") as r:
        check(not await r.json(), f"{label}: no bridges left in Asterisk")

    if context == "kig-ci-tone":
        heard = ints(b"".join(kiosk.frames[-25:]))
        f, rms = frequency(heard, SAMPLE_RATE), level(heard)
        note(f"{label}: kiosk heard {len(kiosk.frames)} frames; last 2 s: {f:.0f} Hz, RMS {rms:.0f}")
        check(400 <= f <= 480 and rms > 300, f"{label}: the kiosk hears Asterisk's 440 Hz tone")


async def phone_call(kiosk: FakeKiosk, calls: CallManager, phones: PhoneDirectory, s, url,
                     extension: str, label: str) -> None:
    """A kiosk calls a SIP phone, as Kiosk Satellite does."""
    for ev in (kiosk.out_open, kiosk.out_closed):
        ev.clear()
    kiosk.out_frames.clear()
    kiosk.out_texts.clear()
    phone = phones.phones[extension]
    call_id = f"ci-{extension}"
    status, body = await kiosk.place_call(phone.port, call_id)
    check((status, body.get("status")) == (200, "ringing"),
          f"{label}: the gateway rings {phone.label} ({body})")
    check(await wait(lambda: kiosk.out_open.is_set(), 15),
          f"{label}: the phone answered and the kiosk opened its audio socket")
    note(f"{label}: signals at the kiosk: {[a for c, a in kiosk.signals if c == call_id]}")
    pos = 0
    for _ in range(int(5000 / 80)):  # talk 5 s, then the kiosk hangs up
        await kiosk.out_talk(tone(1000, pos, FRAME_BYTES // 2))
        pos += FRAME_BYTES // 2
        await asyncio.sleep(0.08)
    await kiosk.out_hang_up()
    check(await wait(lambda: not calls.calls, 5),
          f"{label}: the kiosk hung up and the gateway ended the call")
    await asyncio.sleep(1)
    async with s.get(f"{url}/ari/channels") as r:
        left = await r.json()
    check(not left,
          f"{label}: the gateway hung up the phone; no channels left {[c['name'] for c in left]}")
    if extension == "101":
        heard = ints(b"".join(kiosk.out_frames[-25:]))
        f, rms = frequency(heard, SAMPLE_RATE), level(heard)
        note(f"{label}: kiosk heard {len(kiosk.out_frames)} frames; last 2 s: {f:.0f} Hz, RMS {rms:.0f}")
        check(400 <= f <= 480 and rms > 300, f"{label}: the kiosk hears the phone's 440 Hz tone")


async def click_to_call(kiosk: FakeKiosk, calls: CallManager, s, url, label: str) -> None:
    """The Home Assistant call action: Asterisk rings phone 101; once it answers
    (with ringback meanwhile) the kiosk rings, showing the phone."""
    kiosk.calls.clear()
    kiosk.frames.clear()
    kiosk.texts.clear()
    kiosk.ws_open.clear()
    kiosk.ws_closed.clear()
    events: list[dict] = []
    calls.listeners.append(events.append)
    error = await handle(calls, {"action": "call", "phone": "101", "kiosk": "Kitchen"})
    check(error == "", f"{label}: the action was taken {error}")
    check(await wait(lambda: kiosk.calls, 15), f"{label}: the phone answered and the kiosk rang")
    frm = kiosk.calls[kiosk.last_call()]["from"]
    note(f"{label}: the kiosk shows {frm['name']!r}")
    check(frm["name"] == "CI Tone Phone (101)", f"{label}: the kiosk shows the phone")
    await asyncio.sleep(1)  # ringback on the phone meanwhile
    check(await kiosk.answer() == 200, f"{label}: the kiosk answered")
    await asyncio.wait_for(kiosk.ws_open.wait(), 10)
    await asyncio.sleep(3)  # listen, then the kiosk hangs up
    heard = ints(b"".join(kiosk.frames[-20:]))
    if len(kiosk.frames) < 25:  # what Asterisk saw, before hanging up
        c = next(iter(calls.calls.values()), None)
        if c is not None:
            note(f"{label}: gateway media received {c.media.received if c.media else None}, "
                 f"sent {c.media.sent if c.media else None}; bridge {c.bridge}; phone {c.channel}")
            async with s.get(f"{url}/ari/channels/{c.channel}/rtp_statistics") as r:
                note(f"{label}: phone RTP stats {r.status} {(await r.text())[:400]}")
            async with s.get(f"{url}/ari/bridges/{c.bridge}") as r:
                note(f"{label}: bridge {r.status} {(await r.text())[:400]}")
        async with s.get(f"{url}/ari/channels") as r:
            note(f"{label}: channels {[(x['name'], x['state']) for x in await r.json()]}")
    await kiosk.hang_up()
    f, rms = frequency(heard, SAMPLE_RATE), level(heard)
    note(f"{label}: kiosk heard {len(kiosk.frames)} frames; last 1.6 s: {f:.0f} Hz, RMS {rms:.0f}")
    check(400 <= f <= 480 and rms > 300, f"{label}: the kiosk hears the phone")
    check(len(kiosk.frames) >= 25,
          f"{label}: audio flows at once after answering ({len(kiosk.frames)} of ~37 frames in 3 s)")
    check(await wait(lambda: not calls.calls, 5),
          f"{label}: the kiosk hung up and the gateway ended the call")
    await asyncio.sleep(1)
    async with s.get(f"{url}/ari/channels") as r:
        left = await r.json()
    check(not left,
          f"{label}: the gateway hung up the phone; no channels left {[c['name'] for c in left]}")
    states = [e["state"] for e in events]
    note(f"{label}: events {states}")
    check(states == ["ringing", "answered", "ended"], f"{label}: Home Assistant events in order")
    calls.listeners.remove(events.append)


async def announcement(s, url, kiosk: FakeKiosk, calls: CallManager, label: str) -> None:
    """Dial 800: the kiosk is sent a broadcast and hears the phone's 440 Hz."""
    for ev in (kiosk.ws_open, kiosk.ws_closed):
        ev.clear()
    kiosk.calls.clear()
    kiosk.frames.clear()
    kiosk.texts.clear()
    async with s.post(f"{url}/ari/channels", params={
        "endpoint": "Local/800@default/n", "context": "kig-ci-tone", "extension": "s", "priority": "1",
        "callerId": "Doorbell <201>",
    }) as r:
        check(r.status == 200, f"{label}: Asterisk dialled 800 (HTTP {r.status})")
    check(await wait(lambda: kiosk.ws_open.is_set(), 15), f"{label}: the kiosk took the broadcast")
    c = kiosk.calls[kiosk.last_call()]
    check(c["kind"] == "broadcast", f"{label}: sent as a broadcast")
    note(f"{label}: the kiosk shows {c['from']['name']!r}")
    check(await wait(lambda: kiosk.ws_closed.is_set(), 15), f"{label}: the announcement ended")
    check(any(json.loads(t).get("type") == "end" for t in kiosk.texts),
          f"{label}: the kiosk was told it ended")
    heard = ints(b"".join(kiosk.frames[-25:]))
    f, rms = frequency(heard, SAMPLE_RATE), level(heard)
    note(f"{label}: kiosk heard {len(kiosk.frames)} frames; last 2 s: {f:.0f} Hz, RMS {rms:.0f}")
    check(400 <= f <= 480 and rms > 300, f"{label}: the kiosk hears the announcement")
    check(await wait(lambda: not calls.announcements, 5), f"{label}: the gateway cleaned up")
    await asyncio.sleep(1)
    async with s.get(f"{url}/ari/channels") as r:
        left = await r.json()
    check(not left, f"{label}: no channels left {[x['name'] for x in left]}")


async def encrypted_call(s, url, roster: Roster, calls: CallManager) -> None:
    """A kiosk with Encrypt communications on: HTTPS and WSS only, both ways."""
    label = "call 2c (phone → encrypted kiosk)"
    hall = FakeKiosk(KEY, name="Hall", kid="hall-id", encrypted=True, cert_dir=Path(tempfile.mkdtemp()))
    await hall.start()
    try:
        roster.heard({"id": hall.id, "name": "Hall", "address": "127.0.0.1", "port": hall.port, "tls": True})
        await roster.probe_due()
        check(roster.kiosks[hall.id].status(roster.key_fingerprint) == "ready",
              f"{label}: the kiosk is Ready")
        await wait(lambda: roster.kiosks[hall.id].extra.get("extension"), 5)
        ext = roster.kiosks[hall.id].extra.get("extension", "")
        check(ext == "802", f"{label}: the kiosk has extension 802 ({ext})")
        await one_call(s, url, hall, calls, "kig-ci-tone", label, ext="802")
        check(hall.calls[hall.last_call()]["from"].get("tls") is True,
              f"{label}: the gateway called as encrypted")
    finally:
        roster.kiosks.pop(hall.id, None)
        await hall.close()


async def main(url: str, user: str, password: str) -> None:
    kiosk = FakeKiosk(KEY)
    await kiosk.start()
    prober = HttpProber()
    roster = Roster(prober, key=KEY)
    roster.heard({"id": kiosk.id, "name": "Kitchen", "address": "127.0.0.1", "port": kiosk.port})
    await roster.probe_due()
    ari = Ari(url, user, password, "kiosk-intercom")
    data = Path(tempfile.mkdtemp())
    calls = CallManager(roster, State(data / "s.json"), ari, listen_host="127.0.0.1",
                        certificate=Certificate(data))
    await calls.start()
    stop = asyncio.Event()
    runner = asyncio.ensure_future(ari.run(calls.on_event, stop))
    await wait(lambda: ari.connected, 10)
    check(ari.connected, f"gateway connected to Asterisk {ari.asterisk_version} over ARI {ari.error}")
    check(roster.kiosks[kiosk.id].extra.get("extension") == "801", "kiosk has extension 801")

    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(user, password)) as s:
        await one_call(s, url, kiosk, calls, "kig-ci-tone", "call 1 (phone → kiosk)")
        flush_report()  # one annotation per call: GitHub cuts long ones
        await one_call(s, url, kiosk, calls, "kig-ci-rec", "call 2 (phone → kiosk, recorded)")
        flush_report()  # one annotation per call: GitHub cuts long ones
        await announcement(s, url, kiosk, calls, "call 2b (phone dials 800: announce to all)")
        flush_report()
        await encrypted_call(s, url, roster, calls)
        flush_report()

        phones = PhoneDirectory(
            ari, roster, calls.state, None,
            {"identity": calls.phone_identity, "call": calls.phone_call,
             "signal": calls.phone_signal, "audio": calls.phone_audio},
            listen_host="127.0.0.1",
        )
        calls.phones = phones
        await phones.refresh()
        note("phones Asterisk has: " + ", ".join(
            f"{p['extension']} {p['name']} {p['state']}{' offered' if p['offered'] else ''}"
            for p in phones.snapshot()))
        check({"101", "102"} <= {p["extension"] for p in phones.snapshot() if p["offered"]},
              "the test phones 101 and 102 are offered to kiosks")
        check(phones.phones["101"].label == "CI Tone Phone (101)", "phones are named by their caller ID")
        await phone_call(kiosk, calls, phones, s, url, "101", "call 3 (kiosk → phone 101)")
        flush_report()  # one annotation per call: GitHub cuts long ones
        await phone_call(kiosk, calls, phones, s, url, "102", "call 4 (kiosk → phone 102, recorded)")
        flush_report()  # one annotation per call: GitHub cuts long ones
        await click_to_call(kiosk, calls, s, url, "call 5 (the call action: phone 101, then the kiosk)")
        flush_report()  # one annotation per call: GitHub cuts long ones
        await phones.close()
    flush_report()

    stop.set()
    await runner
    await calls.stop()
    await ari.close()
    await prober.close()
    await kiosk.close()


def analyse_recording(path: str) -> None:
    import wave

    with wave.open(path) as w:
        rate, data = w.getframerate(), w.readframes(w.getnframes())
    samples = ints(data)
    tail = samples[len(samples) // 3:]  # skip set-up
    f, rms = frequency(tail, rate), level(tail)
    note(f"Asterisk recorded {len(samples) / rate:.1f} s at {rate} Hz: {f:.0f} Hz, RMS {rms:.0f}")
    ok = 900 <= f <= 1100 and rms > 300
    check(ok, f"{path}: Asterisk hears the kiosk's 1000 Hz tone")
    flush_report()


if __name__ == "__main__":
    if sys.argv[1] == "--recording":
        for path in sys.argv[2:]:
            analyse_recording(path)  # each must hear the kiosk's 1000 Hz
    else:
        asyncio.run(main(*sys.argv[1:4]))
