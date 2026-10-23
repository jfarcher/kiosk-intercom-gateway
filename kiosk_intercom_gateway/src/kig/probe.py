"""HTTP identity probes."""

from __future__ import annotations

import logging
import ssl

import aiohttp

from .roster import ProbeResult

_LOG = logging.getLogger(__name__)


class HttpProber:
    """GETs a kiosk's identity. The identity route is public information, so a
    kiosk's self-signed admin certificate is accepted for this probe only."""

    def __init__(self, timeout: float = 4.0) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._insecure = ssl.create_default_context()
        self._insecure.check_hostname = False
        self._insecure.verify_mode = ssl.CERT_NONE

    async def __call__(self, url: str) -> ProbeResult:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            kwargs = {"ssl": self._insecure} if url.startswith("https:") else {}
            async with self._session.get(url, allow_redirects=False, **kwargs) as res:
                try:
                    body = await res.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
                return res.status, body if isinstance(body, dict) else None
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            _LOG.debug("probe %s failed: %s", url, err)
            return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
