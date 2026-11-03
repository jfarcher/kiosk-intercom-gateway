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


async def test_big_registry_and_sensor_survives_a_failed_read():
    """A 5 MB entity registry (4.3 MB was seen on a real install) is read, and
    a registry that cannot be read does not stop the sensor."""
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from kig.ha import HomeAssistant
    from kig.roster import Roster

    padding = [{"entity_id": f"sensor.filler_{i}", "platform": "x", "original_name": "x" * 40,
                "device_id": None} for i in range(40000)]
    fail = {"registry": False}
    posted = []

    async def ws(request):
        sock = web.WebSocketResponse(max_msg_size=0)
        await sock.prepare(request)
        await sock.send_json({"type": "auth_required"})
        await sock.receive_json()
        await sock.send_json({"type": "auth_ok"})
        async for msg in sock:
            m = msg.json()
            if fail["registry"]:
                await sock.send_json({"id": m["id"], "type": "result", "success": False,
                                      "error": {"message": "nope"}})
                continue
            result = {"config/entity_registry/list": ENTITIES + padding,
                      "config/device_registry/list": DEVICES,
                      "config/area_registry/list": AREAS}[m["type"]]
            await sock.send_json({"id": m["id"], "type": "result", "success": True, "result": result})
        return sock

    async def state(request):
        if request.method == "POST":
            posted.append(await request.json())
            return web.json_response({})
        return web.json_response({"state": "idle"})

    app = web.Application()
    app.router.add_get("/websocket", ws)
    app.router.add_route("*", "/api/states/{entity}", state)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()

    async def never(_):
        return None

    roster = Roster(never)
    roster.heard({"id": "k1", "name": "Kitchen Tablet", "address": "10.0.0.5", "port": 2324})
    base = f"http://127.0.0.1:{server.port}"
    ha = HomeAssistant(roster, token="t", core_api=f"{base}/api", core_ws=f"{base}/websocket")
    stop = asyncio.Event()
    try:
        task = asyncio.ensure_future(ha.run(stop, registry_interval=0.2, publish_interval=0.2))
        for _ in range(100):
            if posted and roster.kiosks["k1"].ha_area:
                break
            await asyncio.sleep(0.05)
        assert roster.kiosks["k1"].ha_area == "Kitchen" and roster.kiosks["k1"].ha_state == "idle"
        fail["registry"] = True
        before = len(posted)
        for _ in range(100):
            if len(posted) > before + 1:
                break
            await asyncio.sleep(0.05)
        assert len(posted) > before + 1  # still publishing
        assert posted[-1]["attributes"]["kiosks"][0]["extension"] == ""
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
        await server.close()


async def test_call_events_and_sensors_reach_home_assistant():
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from kig.ha import HomeAssistant
    from kig.roster import Roster

    fired, states, deleted = [], {}, []

    async def ws(request):  # an empty registry is enough here
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.send_json({"type": "auth_required"})
        await sock.receive_json()
        await sock.send_json({"type": "auth_ok"})
        async for msg in sock:
            m = msg.json()
            await sock.send_json({"id": m["id"], "type": "result", "success": True, "result": []})
        return sock

    async def event(request):
        fired.append((request.match_info["type"], await request.json()))
        return web.json_response({"message": "Event fired."})

    async def state(request):
        entity = request.match_info["entity"]
        if request.method == "DELETE":
            deleted.append(entity)
            states.pop(entity, None)
        elif request.method == "POST":
            states[entity] = await request.json()
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/websocket", ws)
    app.router.add_post("/api/events/{type}", event)
    app.router.add_route("*", "/api/states/{entity}", state)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()

    phones = [
        {"extension": "101", "name": "Alice (101)", "state": "online", "selected": True, "offered": True,
         "busy": False, "seen_by": ["Kitchen"]},
        {"extension": "102", "name": "Phone 102", "state": "offline", "selected": True, "offered": False,
         "busy": False, "seen_by": []},
        {"extension": "trunk", "name": "trunk", "state": "online", "selected": False, "offered": False,
         "busy": False, "seen_by": []},
    ]

    class Calls:
        def status(self):
            return {"calls": [{"direction": "out", "state": "ringing", "kiosk": "Kitchen", "extension": "801",
                               "caller": "Alice (101)", "since": 1.0}], "phones": phones}

    async def never(_):
        return None

    base = f"http://127.0.0.1:{server.port}"
    ha = HomeAssistant(Roster(never), token="t", core_api=f"{base}/api", core_ws=f"{base}/websocket")
    ha.calls = Calls()
    stop = asyncio.Event()
    task = asyncio.ensure_future(ha.run(stop, registry_interval=60, publish_interval=0.2))
    try:
        ha.call_event({"call_id": "c1", "state": "ringing", "direction": "kiosk_to_phone"})
        for _ in range(100):
            if fired and "binary_sensor.kiosk_intercom_phone_102" in states:
                break
            await asyncio.sleep(0.05)
        assert fired == [("kiosk_intercom_gateway_call",
                          {"call_id": "c1", "state": "ringing", "direction": "kiosk_to_phone"})]
        calls = states["sensor.kiosk_intercom_gateway_calls"]
        assert calls["state"] == 1 and calls["attributes"]["calls"][0]["direction"] == "kiosk_to_phone"
        alice = states["binary_sensor.kiosk_intercom_phone_101"]
        assert alice["state"] == "on" and alice["attributes"]["device_class"] == "connectivity"
        assert alice["attributes"]["found_by"] == ["Kitchen"] and alice["attributes"]["on_kiosks"]
        assert states["binary_sensor.kiosk_intercom_phone_102"]["state"] == "off"
        assert "binary_sensor.kiosk_intercom_phone_trunk" not in states  # not offered to kiosks
        phones.pop(1)  # 102 unticked on the Phones card
        for _ in range(100):
            if "binary_sensor.kiosk_intercom_phone_102" in deleted:
                break
            await asyncio.sleep(0.05)
        assert "binary_sensor.kiosk_intercom_phone_102" in deleted
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
        await server.close()
