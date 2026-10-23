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
from kig.probe import HttpProber  # noqa: E402
from kig.protocol import FRAME_BYTES, SAMPLE_RATE  # noqa: E402
from kig.roster import Roster  # noqa: E402
from kig.state import State  # noqa: E402

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
        raise SystemExit(1)


async def wait(cond, seconds: float) -> bool:
    for _ in range(int(seconds * 10)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def one_call(s, url, kiosk: FakeKiosk, calls: CallManager, context: str, label: str) -> None:
    kiosk.calls.clear()
    kiosk.frames.clear()
    kiosk.texts.clear()
    kiosk.ws_open.clear()
    kiosk.ws_closed.clear()
    async with s.post(f"{url}/ari/channels", params={
        "endpoint": "Local/801@default/n", "context": context, "extension": "s", "priority": "1",
        "callerId": "Doorbell <201>",
    }) as r:
        check(r.status == 200, f"{label}: Asterisk originated a call to 801 (HTTP {r.status})")
    check(await wait(lambda: kiosk.calls, 10), f"{label}: the kiosk rang")
    note(f"{label}: caller shown on the kiosk: " + kiosk.calls[kiosk.last_call()]["from"]["name"])
    check(await kiosk.answer() == 200, f"{label}: the kiosk answered")
    await asyncio.wait_for(kiosk.ws_open.wait(), 10)
    check(True, f"{label}: the gateway opened the kiosk's audio socket")

    # The kiosk talks 1000 Hz in real time until the phone side hangs up.
    pos, stats = 0, None
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
        if i == 40 and call.ext_channel:  # ~3 s in: what Asterisk saw from us
            async with s.get(f"{url}/ari/channels/{call.ext_channel}/rtp_statistics") as r:
                stats = await r.json() if r.status == 200 else {"http": r.status, "body": await r.text()}
    if stats is not None:
        note(f"{label}: Asterisk RTP stats " + json.dumps(stats)[:300])
    if call.rtp:
        note(f"{label}: gateway RTP sent {call.rtp.sent}, received {call.rtp.received}, "
             f"remote {call.rtp.remote}, payload type {call.rtp.payload_type}")

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


async def main(url: str, user: str, password: str) -> None:
    kiosk = FakeKiosk(KEY)
    await kiosk.start()
    prober = HttpProber()
    roster = Roster(prober, key=KEY)
    roster.heard({"id": kiosk.id, "name": "Kitchen", "address": "127.0.0.1", "port": kiosk.port})
    await roster.probe_due()
    ari = Ari(url, user, password, "kiosk-intercom")
    calls = CallManager(roster, State(Path(tempfile.mkdtemp()) / "s.json"), ari, listen_host="127.0.0.1")
    await calls.start()
    stop = asyncio.Event()
    runner = asyncio.ensure_future(ari.run(calls.on_event, stop))
    await wait(lambda: ari.connected, 10)
    check(ari.connected, f"gateway connected to Asterisk {ari.asterisk_version} over ARI {ari.error}")
    check(roster.kiosks[kiosk.id].extra.get("extension") == "801", "kiosk has extension 801")

    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(user, password)) as s:
        await one_call(s, url, kiosk, calls, "kig-ci-tone", "call 1 (phone → kiosk)")
        await one_call(s, url, kiosk, calls, "kig-ci-rec", "call 2 (kiosk → phone)")
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
            try:
                analyse_recording(path)
            except SystemExit:
                if path == sys.argv[-1]:
                    raise
    else:
        asyncio.run(main(*sys.argv[1:4]))
