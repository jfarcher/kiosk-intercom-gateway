"""Entry point: ``python3 -m kig``."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from . import __version__, config
from .discovery import Discovery
from .ha import HomeAssistant
from .probe import HttpProber
from .roster import Roster
from .web import make_app

_LOG = logging.getLogger("kig")


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

    runner = web.AppRunner(make_app(roster, info, cfg.ingress_only), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.web_port).start()
    _LOG.info("status page on port %d", cfg.web_port)

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
