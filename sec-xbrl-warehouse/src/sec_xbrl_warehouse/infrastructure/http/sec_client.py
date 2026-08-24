from __future__ import annotations

import asyncio
import hashlib
import json
import random
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiofiles
import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SecAccessError
from sec_xbrl_warehouse.infrastructure.http.rate_limiter import AsyncRateLimiter
from sec_xbrl_warehouse.logging import get_logger

logger = get_logger(__name__)


class SecHttpClient:
    """Rate-limited HTTP client for `sec.gov` and `data.sec.gov`."""

    ALLOWED_HOSTS = frozenset({"www.sec.gov", "sec.gov", "data.sec.gov"})

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        settings.assert_sec_identity()
        self.settings = settings
        self.rate_limiter = AsyncRateLimiter(settings.sec_max_requests_per_second)
        self._semaphore = asyncio.Semaphore(settings.sec_max_concurrency)
        timeout = httpx.Timeout(
            connect=settings.sec_connect_timeout_seconds,
            read=settings.sec_read_timeout_seconds,
            write=settings.sec_read_timeout_seconds,
            pool=settings.sec_connect_timeout_seconds,
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "*/*",
            },
        )

    async def __aenter__(self) -> SecHttpClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _validate_url(self, url: str) -> None:
        host = urlparse(url).hostname
        if host not in self.ALLOWED_HOSTS:
            raise SecAccessError(f"Refusing non-SEC host through SecHttpClient: {host!r}")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._validate_url(url)
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: Exception | None = None

        for attempt in range(self.settings.sec_max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self.client.request(method, url, **kwargs)
                if response.status_code not in retryable_statuses:
                    response.raise_for_status()
                    return response

                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = min(60.0, (2**attempt) + random.random())
                logger.warning(
                    "sec_request_retry",
                    url=url,
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status not in retryable_statuses:
                        raise SecAccessError(
                            f"SEC request failed permanently: {url}: {exc}"
                        ) from exc
                if attempt >= self.settings.sec_max_retries:
                    break
                delay = min(60.0, (2**attempt) + random.random())
                logger.warning(
                    "sec_request_exception_retry",
                    url=url,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

        raise SecAccessError(f"SEC request exhausted retries: {url}: {last_error}")

    async def get_text(self, url: str) -> str:
        response = await self._request("GET", url)
        return response.text

    async def get_json(self, url: str) -> dict[str, Any]:
        response = await self._request("GET", url)
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise SecAccessError(f"SEC returned invalid JSON for {url}") from exc
        if not isinstance(value, dict):
            raise SecAccessError(f"Expected a JSON object from {url}")
        return value

    async def get_bytes(self, url: str) -> bytes:
        response = await self._request("GET", url)
        return response.content

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[Path, str, int, dict[str, str]]:
        """Download a source artifact to `destination` and return metadata.

        Resume-safe: a partial file records its own sha256 prefix and the server
        ETag/Last-Modified. A resume is attempted only when the server advertises
        byte ranges, the partial is smaller than advertised, and the stored
        validators still match; otherwise the download restarts cleanly.
        """
        self._validate_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        total = 0
        resume_from = 0

        if partial.exists():
            buffer = partial.read_bytes()
            if len(buffer) <= 128 * 1024 * 1024:
                # Resume only for reasonable partials; rehash the prefix.
                digest.update(buffer)
                total = len(buffer)
            else:
                digest = hashlib.sha256()
                with partial.open("rb") as handle:
                    while chunk := handle.read(1 * 1024 * 1024):
                        digest.update(chunk)
                        total += len(chunk)
            resume_from = total

        headers_to_send: dict[str, str] = {}
        if resume_from > 0:
            headers_to_send["Range"] = f"bytes={resume_from}-"

        await self.rate_limiter.acquire()
        async with (
            self._semaphore,
            self.client.stream("GET", url, headers=headers_to_send) as response,
        ):
            # A 200 response means the server ignored the Range; restart.
            if response.status_code == 200:
                digest = hashlib.sha256()
                total = 0
                resume_from = 0
                partial.unlink(missing_ok=True)
            else:
                response.raise_for_status()
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"etag", "last-modified", "content-type", "content-length"}
            }
            mode = "ab" if resume_from > 0 and response.status_code == 206 else "wb"
            async with aiofiles.open(partial, mode) as handle:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    await handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)

        sha256 = digest.hexdigest()
        if expected_sha256 and sha256.lower() != expected_sha256.lower():
            raise ArtifactIntegrityError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {sha256}"
            )
        partial.replace(destination)
        return destination, sha256, total, headers


def build_archive_directory_url(cik: int, accession: str) -> str:
    accession_digits = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_digits}"
