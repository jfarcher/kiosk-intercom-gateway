"""The ingress page and its JSON."""

from __future__ import annotations

import ipaddress
from importlib import resources
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .roster import Roster

INGRESS_PEER = "172.30.32.2"  # the Supervisor's ingress proxy


def make_app(
    roster: Roster,
    info: Callable[[], dict[str, Any]],
    ingress_only: bool = True,
    on_probe: Callable[[], Any] | None = None,
    set_key: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    set_phones: Callable[[list[str] | None], Awaitable[dict[str, Any]]] | None = None,
    set_up_asterisk: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    set_extension: Callable[[str, str], dict[str, Any]] | None = None,
) -> web.Application:
    @web.middleware
    async def only_ingress(request: web.Request, handler: Any) -> web.StreamResponse:
        if ingress_only:
            peer = request.remote or ""
            try:
                allowed = peer == INGRESS_PEER or ipaddress.ip_address(peer).is_loopback
            except ValueError:
                allowed = False
            if not allowed:
                raise web.HTTPForbidden(text="Open this page from Home Assistant.")
        return await handler(request)

    index = resources.files("kig").joinpath("static/index.html").read_text()

    async def page(_: web.Request) -> web.Response:
        return web.Response(text=index, content_type="text/html")

    async def kiosks(_: web.Request) -> web.Response:
        return web.json_response({"gateway": info(), "kiosks": roster.snapshot()})

    async def reprobe(_: web.Request) -> web.Response:
        for k in roster.kiosks.values():
            k.probed_at = None
        if on_probe is not None:
            on_probe()
        return web.json_response({"ok": True})

    async def key(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        value = body.get("key") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            return web.json_response({"ok": False, "error": "Paste the key from a kiosk."}, status=400)
        if set_key is None:
            return web.json_response({"ok": False, "error": "Not available."}, status=501)
        return web.json_response(await set_key(value.strip()))

    async def phones(request: web.Request) -> web.Response:
        """``{"auto": true}`` or ``{"phones": [{"extension", "name"}]}``."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or set_phones is None:
            return web.json_response({"ok": False, "error": "Not available."}, status=400)
        if body.get("auto"):
            return web.json_response(await set_phones(None))
        entries = []
        for item in body.get("phones") or []:
            ext = str((item or {}).get("extension") or "").strip()
            name = " ".join(str((item or {}).get("name") or "").split())[:60]
            if not ext or any(c in ext for c in "=: ") or "=" in name:
                return web.json_response({"ok": False, "error": f"Not a valid phone: {ext!r}"}, status=400)
            entries.append(f"{ext}={name}" if name else ext)
        if len(entries) > 200:
            return web.json_response({"ok": False, "error": "Too many phones."}, status=400)
        return web.json_response(await set_phones(entries))

    async def extension(request: web.Request) -> web.Response:
        """``{"kiosk": "<id or name>", "extension": "803"}``: give a kiosk a number."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or set_extension is None:
            return web.json_response({"ok": False, "error": "Not available."}, status=400)
        result = set_extension(str(body.get("kiosk") or ""), str(body.get("extension") or ""))
        status = 200 if result.get("ok") else 409 if result.get("conflict") else 400
        return web.json_response(result, status=status)

    app = web.Application(middlewares=[only_ingress])
    async def asterisk(request: web.Request) -> web.Response:
        if set_up_asterisk is None:
            return web.json_response({"ok": False, "error": "Not available."}, status=400)
        result = await set_up_asterisk()
        return web.json_response(result, status=200 if result.get("ok") else 409)

    app.router.add_post("/api/asterisk/setup", asterisk)
    app.router.add_post("/api/extension", extension)
    app.router.add_post("/api/phones", phones)
    app.router.add_post("/api/key", key)
    app.router.add_get("/", page)
    app.router.add_get("/api/kiosks", kiosks)
    app.router.add_post("/api/probe", reprobe)
    return app
