"""HTTP client that talks to the local wechat-realtime-reader (receive-only)."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from .settings import settings


class ReaderError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


class ReaderClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), trust_env=False, proxy=None)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        tok = settings.reader_token
        if not tok:
            raise ReaderError(503, "reader token file missing or empty")
        return {"Authorization": f"Bearer {tok}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        accept: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        await self.start()
        assert self._client is not None
        url = f"{settings.reader_base}{path}"
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        # Drop empty params
        clean = None
        if params is not None:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
        kw: Dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = httpx.Timeout(timeout, connect=3.0)
        try:
            r = await self._client.request(method, url, params=clean, headers=headers, **kw)
        except httpx.TimeoutException:
            raise ReaderError(504, "reader timed out (is WeChat data readable? check macOS privacy prompt)")
        except httpx.HTTPError as e:
            raise ReaderError(502, f"reader unreachable: {type(e).__name__}")
        out_headers = {k: v for k, v in r.headers.items() if k.lower() in (
            "content-type", "cache-control", "x-image-variant", "content-range", "accept-ranges", "content-length"
        )}
        return r.status_code, r.content, out_headers

    async def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        status, body, _ = await self.request("GET", path, params=params, accept="application/json")
        if status >= 400:
            detail = body.decode("utf-8", errors="replace")[:500]
            raise ReaderError(status, detail or f"reader HTTP {status}")
        import json
        return json.loads(body.decode("utf-8"))


reader = ReaderClient()
