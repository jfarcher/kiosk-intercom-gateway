import pytest

from kig.protocol import fingerprint
from kig.roster import Roster, parse_manual

KEY = "k"
FP = fingerprint(KEY)


def identity(**over):
    body = {"id": "abc", "name": "Kitchen", "version": "2026.10.1", "enabled": True,
            "key": FP, "dnd": False, "endpoint": {"port": 2325, "tls": False}}
    body.update(over)
    return body


class FakeProber:
    def __init__(self):
        self.answers = {}
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        return self.answers.get(url)


@pytest.fixture
def prober():
    return FakeProber()


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("10.0.0.5", ("10.0.0.5", 2324, False)),
        ("10.0.0.5:2400", ("10.0.0.5", 2400, False)),
        ("https://kiosk.lan:2324/", ("kiosk.lan", 2324, True)),
        ("[fe80::1]:2324", ("fe80::1", 2324, False)),
    ],
)
def test_parse_manual(entry, expected):
    assert parse_manual(entry) == expected


async def test_mdns_kiosk_becomes_ready(prober):
    r = Roster(prober, key=KEY)
    k = r.heard({"id": "abc", "name": "Kitchen", "address": "10.0.0.5", "port": 2324})
    assert k.status(FP) == "unknown"
    prober.answers["http://10.0.0.5:2324/api/intercom/identity"] = (200, identity())
    await r.probe_due()
    assert k.status(FP) == "ready"
    assert k.intercom_url == "http://10.0.0.5:2325"
    assert r.snapshot()[0]["status_text"] == "Ready"


@pytest.mark.parametrize(
    "answer,status",
    [
        ((200, identity(enabled=False)), "off"),
        ((200, identity(key="deadbeef")), "key"),
        ((200, identity(dnd=True)), "dnd"),
        ((200, identity(endpoint={"port": 2325, "tls": True})), "encryption"),
        ((404, None), "off"),
        ((500, None), "unreachable"),
        (None, "unreachable"),
    ],
)
async def test_statuses(prober, answer, status):
    r = Roster(prober, key=KEY)
    k = r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    prober.answers[k.admin_url + "/api/intercom/identity"] = answer
    await r.probe_due()
    assert k.status(FP) == status


async def test_no_key_on_gateway(prober):
    r = Roster(prober, key="")
    k = r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    prober.answers[k.admin_url + "/api/intercom/identity"] = (200, identity())
    await r.probe_due()
    assert k.status(r.key_fingerprint) == "no_key"


async def test_rate_limited_probe_keeps_previous_result(prober):
    r = Roster(prober, key=KEY, probe_interval=10)
    k = r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    url = k.admin_url + "/api/intercom/identity"
    prober.answers[url] = (200, identity())
    await r.probe_due()
    k.probed_at = 0
    prober.answers[url] = (429, None)
    await r.probe_due()
    assert k.status(FP) == "ready" and k.probed_at == 0


async def test_lost_kiosk_goes_offline_and_returns(prober):
    r = Roster(prober, key=KEY)
    k = r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    r.lost("abc")
    assert k.status(FP) == "offline"
    assert r.due() == []
    r.heard({"id": "abc", "address": "10.0.0.9", "port": 2324})
    assert k.address == "10.0.0.9" and k.status(FP) == "unknown"


async def test_manual_kiosk_merges_with_mdns(prober):
    r = Roster(prober, key=KEY)
    m = r.add_manual("10.0.0.5")
    prober.answers["http://10.0.0.5:2324/api/intercom/identity"] = (200, identity())
    await r.probe_due()
    assert list(r.kiosks) == ["abc"] and m.id == "abc" and m.name == "Kitchen"
    r.heard({"id": "abc", "name": "Kitchen", "address": "10.0.0.5", "port": 2324})
    assert len(r.kiosks) == 1
    r.lost("abc")
    assert r.kiosks["abc"].status(FP) == "ready"  # still reachable by address


async def test_manual_before_mdns_announcement_merges(prober):
    r = Roster(prober, key=KEY)
    r.add_manual("10.0.0.5:2324")
    r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    assert list(r.kiosks) == ["abc"] and r.kiosks["abc"].manual


async def test_manual_learning_id_of_known_kiosk(prober):
    r = Roster(prober, key=KEY)
    r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    m = r.add_manual("kiosk.lan")  # same kiosk, by another name
    prober.answers["http://10.0.0.5:2324/api/intercom/identity"] = (200, identity())
    prober.answers["http://kiosk.lan:2324/api/intercom/identity"] = (200, identity())
    await r.probe_due()
    assert len(r.kiosks) == 1 and r.kiosks["abc"].manual and r.kiosks["abc"].heard
    assert r.kiosks["abc"] is m or r.kiosks["abc"].address in ("10.0.0.5", "kiosk.lan")


async def test_find(prober):
    r = Roster(prober, key=KEY)
    r.heard({"id": "abc", "name": "Kitchen", "address": "10.0.0.5", "port": 2324})
    r.heard({"id": "def", "name": "Bedroom", "address": "10.0.0.6", "port": 2324})
    assert r.find("kitchen").id == "abc"
    assert r.find("10.0.0.6").id == "def"
    assert r.find("def").name == "Bedroom"
    assert r.find("garage") is None


async def test_change_listener_errors_do_not_break(prober):
    r = Roster(prober, key=KEY)
    seen = []
    r.on_change(lambda: 1 / 0)
    r.on_change(lambda: seen.append(1))
    r.heard({"id": "abc", "address": "10.0.0.5", "port": 2324})
    assert seen
