"""Problems: when they count, and how Home Assistant hears of them."""

from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from kig.ha import ASTERISK_ENTITY, PROBLEM_ENTITY, HomeAssistant
from kig.health import GONE_GRACE, Health
from kig.probe import HttpProber
from kig.roster import Roster
from kig.state import State


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def kiosk(status, kid="k1", name="Kitchen"):
    return {"id": kid, "name": name, "address": "10.0.0.5", "status": status}


ASTERISK_DOWN = {"enabled": True, "connected": False, "error": "wrong ARI username or password"}


def test_asterisk_down_counts_after_its_grace():
    clock = Clock()
    h = Health(clock)
    assert h.check([], ASTERISK_DOWN) == []  # just started, or Asterisk restarting
    clock.t += 121
    [p] = h.check([], ASTERISK_DOWN)
    assert p.id == "asterisk" and "wrong ARI username" in p.message
    assert h.check([], {"enabled": True, "connected": True}) == []
    clock.t += 200
    assert h.check([], ASTERISK_DOWN) == []  # a new outage starts its own grace
    assert h.check([], {"enabled": False, "connected": False}) == []  # not set up: not a problem


def test_kiosk_problems():
    clock = Clock()
    h = Health(clock)
    assert h.check([kiosk("unreachable")], None) == []  # never Ready here: not one of ours yet
    h.check([kiosk("ready")], None)
    h.check([kiosk("unreachable"), kiosk("key", "k2", "Hall")], None)
    clock.t += 61
    [key] = h.check([kiosk("unreachable"), kiosk("key", "k2", "Hall")], None)
    assert key.id == "key_k2" and "Hall holds a different intercom key" in key.message
    clock.t += GONE_GRACE
    ids = {p.id for p in h.check([kiosk("offline"), kiosk("key", "k2", "Hall")], None)}
    assert ids == {"kiosk_k1", "key_k2"}
    assert h.check([kiosk("dnd"), kiosk("ready", "k2", "Hall")], None) == []


def test_no_key_and_phones():
    clock = Clock()
    h = Health(clock)
    alice = {"extension": "101", "name": "Alice", "selected": True}
    h.check([kiosk("no_key")], None, [{**alice, "state": "online"}])
    clock.t += 601
    got = {p.id: p for p in h.check([kiosk("no_key")], None, [{**alice, "state": "offline"}])}
    assert set(got) == {"no_key"}  # the phone only just went
    clock.t += 601
    got = {p.id: p for p in h.check([kiosk("no_key")], None, [{**alice, "state": "offline"}])}
    assert set(got) == {"no_key", "phone_101"} and not got["phone_101"].notify  # sensor only
    unchosen = [{**alice, "selected": False, "state": "offline"}]
    assert {p.id for p in h.check([], None, unchosen)} == set()


async def test_notifications_come_and_go(tmp_path):
    states, services = {}, []

    async def state(request):
        states[request.match_info["entity"]] = await request.json()
        return web.json_response({})

    async def service(request):
        services.append((request.match_info["service"], (await request.json())["notification_id"]))
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/states/{entity}", state)
    app.router.add_post("/api/services/persistent_notification/{service}", service)
    server = TestServer(app)
    await server.start_server()
    prober = HttpProber()
    try:
        roster = Roster(prober, key="k")
        ha = HomeAssistant(roster, token="t", core_api=str(server.make_url("/api")))
        ha.state = State(tmp_path / "s.json")
        clock = Clock()
        ha.health = Health(clock)

        class Calls:
            status_value = dict(ASTERISK_DOWN)

            def status(self):
                return self.status_value

        ha.calls = Calls()
        import aiohttp
        ha._session = aiohttp.ClientSession()
        try:
            await ha.publish_health()
            assert states[PROBLEM_ENTITY]["state"] == "off" and states[ASTERISK_ENTITY]["state"] == "off"
            clock.t += 121
            await ha.publish_health()
            await ha.publish_health()  # once only
            assert services == [("create", "kiosk_intercom_gateway_asterisk")]
            assert states[PROBLEM_ENTITY]["state"] == "on"
            assert states[PROBLEM_ENTITY]["attributes"]["problems"][0]["kind"] == "asterisk"
            assert State(tmp_path / "s.json").notified == ["asterisk"]  # survives a restart

            # The gateway restarts, Asterisk comes back meanwhile: the old one is still dismissed.
            ha2 = HomeAssistant(roster, token="t", core_api=ha.core_api)
            ha2.state, ha2.calls, ha2._session = State(tmp_path / "s.json"), Calls(), ha._session
            ha2.calls.status_value = {"enabled": True, "connected": True, "asterisk_version": "22"}
            await ha2.publish_health()
            assert services[-1] == ("dismiss", "kiosk_intercom_gateway_asterisk")
            assert states[PROBLEM_ENTITY]["state"] == "off" and states[ASTERISK_ENTITY]["state"] == "on"
            assert State(tmp_path / "s.json").notified == []

            ha2.notify = False  # notifications off: the sensor still tells
            ha2.health = Health(clock)
            ha2.calls.status_value = dict(ASTERISK_DOWN)
            await ha2.publish_health()
            clock.t += 121
            await ha2.publish_health()
            assert states[PROBLEM_ENTITY]["state"] == "on" and services[-1][0] == "dismiss"
        finally:
            await ha._session.close()
    finally:
        await prober.close()
        await server.close()
        await asyncio.sleep(0)
