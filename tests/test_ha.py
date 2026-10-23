from kig.ha import intercom_devices, match

ENTITIES = [
    {"entity_id": "sensor.kitchen_tablet_intercom", "platform": "esphome",
     "original_name": "Intercom", "device_id": "d1"},
    {"entity_id": "sensor.bedroom_intercom", "platform": "esphome",
     "original_name": "Intercom", "device_id": "d2"},
    {"entity_id": "sensor.kitchen_tablet_battery", "platform": "esphome",
     "original_name": "Battery", "device_id": "d1"},
    {"entity_id": "sensor.other_intercom", "platform": "template",
     "original_name": "Intercom", "device_id": "d3"},
]
DEVICES = [
    {"id": "d1", "name": "kitchen-tablet", "name_by_user": "Kitchen Tablet", "area_id": "kitchen"},
    {"id": "d2", "name": "bedroom", "name_by_user": None, "area_id": None},
    {"id": "d3", "name": "x", "area_id": None},
]
AREAS = [{"area_id": "kitchen", "name": "Kitchen"}]


def test_finds_only_kiosk_devices():
    devs = intercom_devices(ENTITIES, DEVICES, AREAS)
    assert [d["device_id"] for d in devs] == ["d1", "d2"]
    assert devs[0]["area"] == "Kitchen" and devs[0]["name"] == "Kitchen Tablet"
    assert devs[0]["intercom_entity"] == "sensor.kitchen_tablet_intercom"


def test_matches_by_normalised_name():
    devs = intercom_devices(ENTITIES, DEVICES, AREAS)
    assert match(["Kitchen Tablet"], devs)["device_id"] == "d1"
    assert match(["kitchen_tablet"], devs)["device_id"] == "d1"
    assert match(["Bedroom"], devs)["device_id"] == "d2"
    assert match(["Garage", ""], devs) is None


def test_ambiguous_match_is_no_match():
    devs = intercom_devices(ENTITIES, DEVICES, AREAS)
    assert match(["kitchen tablet", "bedroom"], devs) is None
