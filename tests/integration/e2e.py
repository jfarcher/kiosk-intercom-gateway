"""A real call through a real Asterisk (the Home Assistant Asterisk app's
image) into a fake kiosk. Run by CI after the container is up:

    python3 tests/integration/e2e.py http://127.0.0.1:8088 kig ci-secret
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


def note(text: str) -> None:
    """Printed, and an annotation on GitHub Actions (readable without the logs)."""
    print(text, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=e2e::{text}", flush=True)


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{'notice' if ok else 'error'} title=e2e::{'PASS' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        raise SystemExit(1)


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
    for _ in range(100):
        if ari.connected:
            break
        await asyncio.sleep(0.1)
    check(ari.connected, f"gateway connected to Asterisk {ari.asterisk_version} over ARI ({ari.error})")
    check(roster.kiosks[kiosk.id].extra.get("extension") == "801", "kiosk has extension 801")

    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(user, password)) as s:
        async with s.post(f"{url}/ari/channels", params={
            "endpoint": "Local/801@default/n", "context": "kig-ci", "extension": "s", "priority": "1",
            "callerId": "Doorbell <201>",
        }) as r:
            check(r.status == 200, f"Asterisk originated a call to 801 (HTTP {r.status})")

        for _ in range(100):
            if kiosk.calls:
                break
            await asyncio.sleep(0.1)
        check(bool(kiosk.calls), "the kiosk rang")
        note("caller shown on the kiosk: " + kiosk.calls[kiosk.last_call()]["from"]["name"])
        check(await kiosk.answer() == 200, "the kiosk answered")
        await asyncio.wait_for(kiosk.ws_open.wait(), 10)
        check(True, "the gateway opened the kiosk's audio socket")

        # Talk 1000 Hz at Asterisk for 5 s, in real time, while hearing it.
        pos = 0
        for _ in range(int(5000 / 80)):
            if kiosk.ws_closed.is_set():
                break
            await kiosk.talk(tone(1000, pos, FRAME_BYTES // 2))
            pos += FRAME_BYTES // 2
            await asyncio.sleep(0.08)

        heard = ints(b"".join(kiosk.frames[-25:]))  # the last two seconds
        rms = math.sqrt(sum(x * x for x in heard) / max(1, len(heard)))
        f = frequency(heard, SAMPLE_RATE)
        note(f"kiosk heard {len(kiosk.frames)} frames; last 2 s: {f:.0f} Hz, RMS {rms:.0f}")
        check(all(len(x) == FRAME_BYTES for x in kiosk.frames), "every frame to the kiosk is 80 ms")
        check(400 <= f <= 480 and rms > 300, "the kiosk hears Asterisk's 440 Hz tone")

        # The phone side records 6 s and hangs up; the kiosk must hear the end.
        await asyncio.wait_for(kiosk.ws_closed.wait(), 15)
        check(any(json.loads(t).get("type") == "end" for t in kiosk.texts),
              "the kiosk was told the caller hung up")
        for _ in range(50):
            if not calls.calls:
                break
            await asyncio.sleep(0.1)
        check(not calls.calls, "the gateway cleaned the call up")
        await asyncio.sleep(1)
        async with s.get(f"{url}/ari/channels") as r:
            left = await r.json()
        check(not left, f"no channels left in Asterisk ({[c['name'] for c in left]})")
        async with s.get(f"{url}/ari/bridges") as r:
            check(not await r.json(), "no bridges left in Asterisk")

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
    rms = math.sqrt(sum(x * x for x in tail) / max(1, len(tail)))
    f = frequency(tail, rate)
    note(f"Asterisk recorded {len(samples) / rate:.1f} s at {rate} Hz: {f:.0f} Hz, RMS {rms:.0f}")
    check(900 <= f <= 1100 and rms > 300, "Asterisk hears the kiosk's 1000 Hz tone")


if __name__ == "__main__":
    if sys.argv[1] == "--recording":
        analyse_recording(sys.argv[2])
    else:
        asyncio.run(main(*sys.argv[1:4]))
