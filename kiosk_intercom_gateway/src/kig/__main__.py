"""Entry point: ``python3 -m kig``."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from . import __version__, config
from .discovery import Discovery
from .ha import HomeAssistant, save_options, supervisor_ingress_port
from .probe import HttpProber
from .roster import Roster
from .web import make_app

_LOG = logging.getLogger("kig")
DEFAULT_WEB_PORT = 8099  # outside the Supervisor


async def main() -> None:
    cfg = config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOG.info("Kiosk Intercom Gateway %s starting", __version__)
    if not cfg.intercom_key:
        _LOG.warning("no intercom key set: kiosks will be listed but none reads Ready")

    prober = HttpProber()
    roster = Roster(prober, key=cfg.intercom_key, probe_interval=cfg.probe_interval)
    for entry in cfg.extra_kiosks:
        try:
            roster.add_manual(entry)
        except ValueError:
            _LOG.warning("ignoring extra kiosk %r: expected host or host:port", entry)

    ha = HomeAssistant(roster)
    roster.on_change(ha.changed)

    discovery = Discovery(roster) if cfg.mdns else None
    if discovery:
        await discovery.start()

    def info() -> dict:
        return {
            "version": __version__,
            "key_fingerprint": roster.key_fingerprint,
            "discovery": discovery is not None,
            "home_assistant": ha.available,
        }

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    port = cfg.web_port
    if not port and config.under_supervisor():
        try:
            port = await supervisor_ingress_port()
        except Exception as err:
            port = None
            _LOG.error("could not read the ingress port from the Supervisor: %s", err)
        if not port:
            _LOG.error("no ingress port from the Supervisor (token %s); not guessing one",
                       "present" if config.supervisor_token() else "missing")
            if discovery:
                await discovery.stop()
            await prober.close()
            raise SystemExit(1)
    port = port or DEFAULT_WEB_PORT
    async def set_key(key: str) -> dict:
        """Applies a key at once and keeps it in the app's configuration."""
        roster.set_key(key)
        _LOG.info("intercom key changed (fingerprint %s)", roster.key_fingerprint)
        if not config.under_supervisor():
            return {"ok": True, "saved": False, "fingerprint": roster.key_fingerprint,
                    "note": "Applied until restart (not running under Home Assistant)."}
        try:
            await save_options({"intercom_key": key}, config.raw_options())
        except Exception as err:
            _LOG.warning("could not save the key to the configuration: %s", err)
            return {"ok": True, "saved": False, "fingerprint": roster.key_fingerprint,
                    "note": f"Applied, but not saved ({err}). Set it under Configuration too."}
        return {"ok": True, "saved": True, "fingerprint": roster.key_fingerprint}

    runner = web.AppRunner(
        make_app(roster, info, cfg.ingress_only, set_key=set_key), access_log=None
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except OSError as err:
        _LOG.error("status page cannot listen on port %d: %s (another service on the host holds it)",
                   port, err.strerror or err)
        if discovery:
            await discovery.stop()
        await runner.cleanup()
        await prober.close()
        raise SystemExit(1) from err
    _LOG.info("status page on port %d", port)

    try:
        await asyncio.gather(roster.run(stop), ha.run(stop))
    finally:
        if discovery:
            await discovery.stop()
        await runner.cleanup()
        await prober.close()
        _LOG.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
