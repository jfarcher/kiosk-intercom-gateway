"""Kiosks that are gone for good: Forget, and the tidy-up after a week."""

from __future__ import annotations

from kig.__main__ import _endpoint
from kig.health import GONE_GRACE, Health
from kig.roster import TIDY_AFTER, Roster
from kig.state import State
from test_bridge import Rig, until


class NoProbe:
    async def identity(self, *_a, **_k):
        raise OSError("gone")

    async def close(self):
        pass


def roster_with(*ids):
    r = Roster(NoProbe(), key="k")
    for kid in ids:
        r.heard({"id": kid, "name": kid.title(), "address": f"10.0.0.{len(r.kiosks) + 5}", "port": 2324})
    return r


def test_tidy_drops_a_kiosk_offline_for_a_week():
    r = roster_with("kitchen", "hall")
    r.lost("kitchen")
    assert r.tidy(now=1000.0) == []
    assert r.kiosks["kitchen"].away_since == 1000.0
    assert r.kiosks["kitchen"].to_json(r.key_fingerprint)["away_since"] == 1000.0
    assert r.tidy(now=1000.0 + TIDY_AFTER - 1) == []
    r.heard({"id": "kitchen", "name": "Kitchen", "address": "10.0.0.5", "port": 2324})  # back
    r.tidy(now=1000.0 + TIDY_AFTER)
    assert r.kiosks["kitchen"].away_since is None
    r.lost("kitchen")
    r.tidy(now=5000.0)
    [gone] = r.tidy(now=5000.0 + TIDY_AFTER)
    assert gone.id == "kitchen" and set(r.kiosks) == {"hall"}


def test_tidy_keeps_kiosks_added_by_address_and_unreachable_ones():
    r = roster_with("hall")
    r.add_manual("10.9.9.9")
    manual = next(k for k in r.kiosks.values() if k.manual)
    manual.probe_failed = True  # Unreachable, but it's configuration
    hall = r.kiosks["hall"]
    hall.probe_failed = True  # still announcing, not answering: it would come straight back
    assert hall.status(r.key_fingerprint) == "unreachable"
    r.tidy(now=0.0)
    assert r.tidy(now=TIDY_AFTER * 10) == [] and len(r.kiosks) == 2
    assert hall.away_since == 0.0


async def test_forget_frees_the_number(tmp_path):
    rig = await Rig(tmp_path).start()
    try:
        await until(lambda: rig.roster.kiosks[rig.kiosk.id].extra.get("extension") == "801")
        refused = rig.calls.forget_kiosk("Kitchen")
        assert not refused["ok"] and "is here (Ready)" in refused["error"]

        rig.roster.lost(rig.kiosk.id)
        result = rig.calls.forget_kiosk(rig.kiosk.id)
        assert result == {"ok": True, "kiosk": "Kitchen", "extension": "801", "manual": False,
                          "address": "127.0.0.1", "port": rig.kiosk.port}
        assert rig.kiosk.id not in rig.roster.kiosks
        saved = State(rig.calls.state.path)
        assert rig.kiosk.id not in saved.extensions and rig.kiosk.id not in saved.addresses
        assert saved.extension_for("new-kiosk", 801) == "801"  # free again
        assert not rig.calls.forget_kiosk("Kitchen")["ok"]  # nothing left to forget
    finally:
        await rig.close()


async def test_forgotten_kiosk_clears_its_problem(tmp_path):
    r = roster_with("kitchen")
    r.kiosks["kitchen"].enabled, r.kiosks["kitchen"].fingerprint = True, r.key_fingerprint
    t = [0.0]
    h = Health(lambda: t[0])
    h.check(r.snapshot(), None)
    r.lost("kitchen")
    h.check(r.snapshot(), None)
    t[0] += GONE_GRACE
    assert [p.id for p in h.check(r.snapshot(), None)] == ["kiosk_kitchen"]
    r.forget("kitchen")
    assert h.check(r.snapshot(), None) == []  # so its notification is dismissed


def test_extra_kiosk_entries_match_by_endpoint():
    assert _endpoint("10.0.0.9") == ("10.0.0.9", 2324)
    assert _endpoint("10.0.0.9:2400") == ("10.0.0.9", 2400)
    assert _endpoint("https://hall.local:2324") == ("hall.local", 2324)
