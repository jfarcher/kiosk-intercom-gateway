from kig.discovery import SERVICE, instance_id, parse_service


def test_parses_kiosk_announcement():
    peer = parse_service(
        "ks-abc." + SERVICE,
        {b"id": b"abc", b"name": b"Kitchen", b"version": b"2026.10.1", b"port": b"2324",
         b"host": b"kitchen.local", b"tls": b"1"},
        ["169.254.1.1", "fe80::1", "192.168.1.20"],
        9999,
    )
    assert peer == {"id": "abc", "name": "Kitchen", "version": "2026.10.1",
                    "address": "192.168.1.20", "port": 2324, "tls": True, "host": "kitchen.local"}


def test_falls_back_to_instance_name_and_srv_port():
    peer = parse_service("ks-xyz." + SERVICE, {}, ["10.0.0.7"], 2400)
    assert peer["id"] == "xyz" and peer["port"] == 2400 and peer["name"] == "ks-xyz"
    assert peer["tls"] is False


def test_ignores_unusable():
    assert parse_service("ks-a." + SERVICE, {b"id": b"a"}, ["127.0.0.1"], 2324) is None


def test_instance_id():
    assert instance_id("ks-abc." + SERVICE) == "abc"


async def test_phone_announcement_is_one_kiosk_style_packet():
    """Kiosks only take an announcement whose PTR, SRV, TXT and A arrive in one
    packet (FleetDiscovery.kt), and re-announcing is ours to do every 30 s."""
    from zeroconf import DNSIncoming

    from kig.discovery import Discovery
    from kig.roster import Roster

    async def never(_):
        return None

    d = Discovery(Roster(never), browse=False, ignore_prefix="kig-abc")
    await d.start()
    sent = []
    try:
        d._azc.zeroconf.async_send = lambda out, *a, **k: sent.append(out)
        info = d.service_info("kig-abc-101", "Alice (101)", "192.168.1.5", 40001, "0.3.0", "kig-abc")
        await d.advertise({"kig-abc-101": info})
        sent.clear()
        d.announce()
        packets = [p for out in sent for p in out.packets()]
        assert len(packets) == 1
        records = DNSIncoming(packets[0]).answers()
        kinds = {type(r).__name__ for r in records}
        assert {"DNSPointer", "DNSService", "DNSText", "DNSAddress"} <= kinds
        srv = next(r for r in records if type(r).__name__ == "DNSService")
        assert srv.port == 40001 and srv.server == "kig-abc.local."
        txt = next(r for r in records if type(r).__name__ == "DNSText").text
        assert b"id=kig-abc-101" in txt and b"port=40001" in txt and b"name=Alice (101)" in txt
        ptr = next(r for r in records if type(r).__name__ == "DNSPointer")
        assert ptr.alias == f"ks-kig-abc-101.{SERVICE}"
        # Ours, heard back, is not a kiosk.
        assert d._ours("kig-abc-101") and not d._ours("kitchen-id")
        # Withdrawn phones say goodbye and stop being announced.
        await d.advertise({})
        sent.clear()
        d.announce()
        assert not sent
    finally:
        await d.stop()


async def test_renaming_a_phone_sends_no_goodbye():
    """A goodbye (TTL 0) for the same instance would trail the new records and
    make every kiosk drop the phone until the next 30-second announcement."""
    import asyncio

    from zeroconf import DNSIncoming

    from kig.discovery import Discovery
    from kig.roster import Roster

    async def never(_):
        return None

    d = Discovery(Roster(never), browse=False, ignore_prefix="kig-abc")
    await d.start()
    sent = []
    try:
        d._azc.zeroconf.async_send = lambda out, *a, **k: sent.append(out)
        first = d.service_info("kig-abc-101", "Alice (101)", "192.168.1.5", 40001, "0.3.1", "kig-abc")
        await d.advertise({"kig-abc-101": first})
        await asyncio.sleep(1)
        sent.clear()
        renamed = d.service_info("kig-abc-101", "Front door (101)", "192.168.1.5", 40001, "0.3.1", "kig-abc")
        await d.advertise({"kig-abc-101": renamed})
        await asyncio.sleep(1)  # the update's own rebroadcasts
        records = [r for out in sent for p in out.packets() for r in DNSIncoming(p).answers()]
        assert records and not [r for r in records if r.ttl == 0]
        texts = [r.text for r in records if type(r).__name__ == "DNSText"]
        assert texts and all(b"name=Front door (101)" in t for t in texts)
    finally:
        await d.stop()
