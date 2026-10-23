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
