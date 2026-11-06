"""What's wrong right now, for Home Assistant and the status page.

Each problem has a stable id, so Home Assistant gets one notification per
problem, dismissed by the gateway once it's fixed. Short blips don't count:
a condition becomes a problem only after it has lasted its grace time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

ASTERISK_GRACE = 120.0  # Asterisk restarting, or the gateway just started
KEY_GRACE = 60.0  # a key being changed across the kiosks
GONE_GRACE = 600.0  # a kiosk rebooting or updating
PHONE_GRACE = 600.0


@dataclass
class Problem:
    id: str
    kind: str  # asterisk, no_key, key, kiosk, phone
    title: str
    message: str
    since: float
    notify: bool = True  # phones come and go (softphones): the sensor only

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Health:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._first: dict[str, float] = {}  # condition id -> when it was first seen
        self._ready_once: set[str] = set()  # kiosks seen Ready since the gateway started
        self._online_once: set[str] = set()  # phones seen registered
        self.problems: list[Problem] = []

    def check(self, kiosks: list[dict[str, Any]], asterisk: dict[str, Any] | None,
              phones: list[dict[str, Any]] | None = None) -> list[Problem]:
        now = self.clock()
        seen: dict[str, tuple[float, Problem]] = {}

        def condition(pid: str, grace: float, kind: str, title: str, message: str,
                      notify: bool = True) -> None:
            since = self._first.setdefault(pid, now)
            seen[pid] = (grace, Problem(pid, kind, title, message, since, notify))

        if asterisk and asterisk.get("enabled") and not asterisk.get("connected"):
            why = asterisk.get("error") or "not connected"
            condition("asterisk", ASTERISK_GRACE, "asterisk", "Kiosk intercom: Asterisk not connected",
                      f"Phones can't call the kiosks, or the kiosks the phones: {why}.")

        if any(k.get("status") == "no_key" for k in kiosks):
            condition("no_key", KEY_GRACE, "no_key", "Kiosk intercom: no intercom key",
                      "The gateway has no intercom key, so no kiosk can be called. "
                      "Paste the kiosks' key on the app's page.")
        for k in kiosks:
            kid = k.get("id") or k.get("address", "")
            name, status = k.get("name") or k.get("address", ""), k.get("status")
            if status in ("ready", "dnd"):
                self._ready_once.add(kid)
            if status == "key":
                condition(f"key_{kid}", KEY_GRACE, "key", f"Kiosk intercom: {name} has a different key",
                          f"{name} holds a different intercom key from the gateway, so it can't be called "
                          "and can't call the phones.")
            elif status in ("unreachable", "offline") and kid in self._ready_once:
                what = "stopped answering" if status == "unreachable" else "stopped announcing itself"
                condition(f"kiosk_{kid}", GONE_GRACE, "kiosk", f"Kiosk intercom: {name} is unavailable",
                          f"{name} {what}, so calls to it fail. Is it switched on and on the network?")

        for p in phones or []:
            ext = str(p.get("extension", ""))
            if not p.get("selected"):
                continue
            if p.get("state") == "online":
                self._online_once.add(ext)
            elif ext in self._online_once:
                name = p.get("name") or ext
                condition(f"phone_{ext}", PHONE_GRACE, "phone", f"Kiosk intercom: {name} not registered",
                          f"{name} ({ext}) stopped registering with Asterisk, so it's off the kiosks' list.",
                          notify=False)

        for pid in list(self._first):
            if pid not in seen:
                del self._first[pid]
        self.problems = [p for grace, p in seen.values() if now - p.since >= grace]
        return self.problems
