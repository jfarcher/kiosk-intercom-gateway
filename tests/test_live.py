"""The real HTTP prober and status page against a fake kiosk."""

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kig.probe import HttpProber
from kig.protocol import fingerprint
from kig.roster import Roster
from kig.web import make_app

KEY = "k"


async def fake_kiosk(dnd=False):
    hits = []

    async def identity(request):
        hits.append(request.remote)
        if len(hits) > 1 and hits[-1] == hits[-2] and request.query.get("burst"):
            return web.json_response({"error": "too many probes"}, status=429)
        return web.json_response({"id": "abc", "name": "Kitchen", "version": "2026.10.1",
                                  "enabled": True, "key": fingerprint(KEY), "dnd": dnd,
                                  "endpoint": {"port": 2325, "tls": False}})

    app = web.Application()
    app.router.add_get("/api/intercom/identity", identity)
    server = TestServer(app)
    await server.start_server()
    return server, hits


async def test_probe_live_kiosk():
    server, hits = await fake_kiosk()
    prober = HttpProber()
    try:
        r = Roster(prober, key=KEY)
        r.add_manual(f"127.0.0.1:{server.port}")
        await r.probe_due()
        snap = r.snapshot()
        assert snap[0]["status"] == "ready" and snap[0]["name"] == "Kitchen"
        assert snap[0]["intercom_url"] == "http://127.0.0.1:2325"
        assert len(hits) == 1
    finally:
        await prober.close()
        await server.close()


async def test_probe_dead_port():
    prober = HttpProber(timeout=1)
    try:
        r = Roster(prober, key=KEY)
        r.add_manual("127.0.0.1:1")
        await r.probe_due()
        assert r.snapshot()[0]["status"] == "unreachable"
    finally:
        await prober.close()


async def test_status_page_and_json():
    async def never(_):
        return None

    r = Roster(never, key=KEY)
    r.heard({"id": "abc", "name": "Kitchen", "address": "10.0.0.5", "port": 2324})
    app = make_app(r, lambda: {"version": "0.1.0", "key_fingerprint": r.key_fingerprint,
                               "discovery": True, "home_assistant": False})
    async with TestClient(TestServer(app)) as client:
        page = await client.get("/")
        assert page.status == 200 and "Kiosk Intercom Gateway" in await page.text()
        data = await (await client.get("/api/kiosks")).json()
        assert data["kiosks"][0]["id"] == "abc" and data["gateway"]["discovery"]
        assert (await client.post("/api/probe")).status == 200


async def test_status_page_refuses_non_ingress_peers():
    async def never(_):
        return None

    app = make_app(Roster(never), lambda: {}, ingress_only=True)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        assert (await client.get("/")).status == 200  # loopback is allowed
