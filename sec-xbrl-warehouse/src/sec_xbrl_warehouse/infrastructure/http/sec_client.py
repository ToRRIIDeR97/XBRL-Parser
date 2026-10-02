from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiofiles
import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SecAccessError, SecRetryDeferred
from sec_xbrl_warehouse.infrastructure.http.rate_limiter import AsyncRateLimiter
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.observability.metrics import SEC_REQUESTS, SOURCE_BYTES

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
            follow_redirects=False,
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
        try:
            parsed = urlparse(url)
            if parsed.hostname not in self.ALLOWED_HOSTS:
                raise SecAccessError(
                    f"Refusing non-SEC host through SecHttpClient: {parsed.hostname!r}"
                )
            if (
                parsed.scheme != "https"
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise SecAccessError("SEC URLs must use HTTPS port 443 without credentials")
        except ValueError as exc:
            raise SecAccessError("Invalid SEC URL") from exc

    async def _buffered_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self.client.stream(method, url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > self.settings.response_max_bytes:
                    raise ArtifactIntegrityError("SEC response exceeds configured byte limit")
                body.extend(chunk)
            headers = dict(response.headers)
            if method != "HEAD":
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.settings.assert_sec_identity()
        self._validate_url(url)
        kwargs["follow_redirects"] = False
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: Exception | None = None

        for attempt in range(self.settings.sec_max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self._buffered_request(method, url, **kwargs)
                _observe_response(method, url, response)
                for _ in range(10):
                    if not response.is_redirect:
                        break
                    redirected = urljoin(url, response.headers["location"])
                    self._validate_url(redirected)
                    await response.aclose()
                    url = redirected
                    await self.rate_limiter.acquire()
                    async with self._semaphore:
                        response = await self._buffered_request(method, url, **kwargs)
                    _observe_response(method, url, response)
                if response.is_redirect:
                    raise SecAccessError(f"Too many SEC redirects: {url}")
                self._validate_url(str(response.url))
                if response.status_code not in retryable_statuses:
                    response.raise_for_status()
                    return response

                await response.aclose()
                delay = _retry_delay(response.headers.get("Retry-After"), attempt)
                logger.warning(
                    "sec_request_retry",
                    url=url,
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                )
                last_error = SecAccessError(f"Retryable SEC response {response.status_code}: {url}")
                await response.aclose()
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                if not isinstance(exc, httpx.HTTPStatusError):
                    SEC_REQUESTS.labels(
                        host=urlparse(url).hostname or "unknown", status="error"
                    ).inc()
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

    async def head(self, url: str) -> dict[str, str]:
        response = await self._request("HEAD", url)
        return {key.lower(): value for key, value in response.headers.items()}

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> tuple[Path, str, int, dict[str, str]]:
        """Download a source artifact to `destination` and return metadata.

        Resume-safe: a partial file records its size and the server
        ETag/Last-Modified. A resume re-hashes the partial and uses If-Range;
        malformed ranges, changed validators, and incomplete bodies are rejected.
        """
        self._validate_url(url)
        self.settings.assert_sec_identity()
        byte_limit = (
            min(max_bytes, self.settings.download_max_bytes)
            if max_bytes is not None
            else self.settings.download_max_bytes
        )
        if byte_limit <= 0:
            raise ArtifactIntegrityError("Download byte budget exhausted")
        await asyncio.to_thread(self.settings.require_disk_reserve)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        sidecar = partial.with_suffix(partial.suffix + ".json")
        digest = hashlib.sha256()
        total = 0
        resume_from = 0
        prior: dict[str, Any] = {}

        if partial.exists() and sidecar.is_file():
            try:
                prior = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prior = {}
        try:
            reusable_partial = (
                prior.get("url") == url and int(prior.get("size", -1)) == partial.stat().st_size
            )
        except (OSError, TypeError, ValueError):
            reusable_partial = False
        if reusable_partial:
            if partial.stat().st_size > byte_limit:
                raise ArtifactIntegrityError("Partial download exceeds configured byte limit")
            async with aiofiles.open(partial, "rb") as partial_handle:
                while chunk := await partial_handle.read(1024 * 1024):
                    digest.update(chunk)
                    total += len(chunk)
            resume_from = total
        else:
            partial.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            prior = {}

        headers_to_send: dict[str, str] = {}
        if resume_from > 0:
            headers_to_send["Range"] = f"bytes={resume_from}-"
            validator = prior.get("etag") or prior.get("last-modified")
            if validator:
                headers_to_send["If-Range"] = validator
        headers_to_send["Accept-Encoding"] = "identity"

        expected_total: int | None = None
        async with self._stream_response(url, headers_to_send) as response:
            # A 200 response means the server ignored the Range; restart.
            if response.status_code == 200:
                digest = hashlib.sha256()
                total = 0
                resume_from = 0
                partial.unlink(missing_ok=True)
                expected_total = _positive_int(response.headers.get("content-length"))
            elif response.status_code == 416:
                expected_total = _content_range_total(response.headers.get("content-range"))
                validators_match = all(
                    not prior.get(name) or response.headers.get(name) == prior[name]
                    for name in ("etag", "last-modified")
                )
                if resume_from and expected_total == resume_from and validators_match:
                    sha256 = digest.hexdigest()
                    if expected_sha256 and sha256.lower() != expected_sha256.lower():
                        raise ArtifactIntegrityError(
                            f"SHA-256 mismatch for complete partial {url}: {sha256}"
                        )
                    partial.replace(destination)
                    sidecar.unlink(missing_ok=True)
                    return destination, sha256, total, _download_headers(response)
                partial.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                raise ArtifactIntegrityError(f"SEC rejected an invalid byte range for {url}")
            else:
                response.raise_for_status()
                content_range = _content_range(response.headers.get("content-range"))
                if (
                    response.status_code != 206
                    or not content_range
                    or content_range[0] != resume_from
                ):
                    raise ArtifactIntegrityError(f"Malformed resume response for {url}")
                start, end, expected_total = content_range
                content_length = _positive_int(response.headers.get("content-length"))
                if content_length is not None and content_length != end - start + 1:
                    raise ArtifactIntegrityError(
                        f"Malformed Content-Length for resumed download: {url}"
                    )
                for name in ("etag", "last-modified"):
                    if prior.get(name) and response.headers.get(name) != prior[name]:
                        raise ArtifactIntegrityError(
                            f"{name} changed during resumed download: {url}"
                        )
            if expected_total is not None and expected_total > byte_limit:
                raise ArtifactIntegrityError("Download exceeds configured byte limit")
            headers = _download_headers(response)
            mode = "ab" if resume_from > 0 and response.status_code == 206 else "wb"
            await asyncio.to_thread(
                sidecar.write_text,
                json.dumps(
                    {
                        "url": url,
                        "size": total,
                        "etag": response.headers.get("etag"),
                        "last-modified": response.headers.get("last-modified"),
                    }
                ),
                encoding="utf-8",
            )
            async with aiofiles.open(partial, mode) as handle:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if total + len(chunk) > byte_limit:
                        raise ArtifactIntegrityError("Download exceeds configured byte limit")
                    await handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    SOURCE_BYTES.labels(
                        source=urlparse(url).hostname or "unknown", dataset="sec"
                    ).inc(len(chunk))
                    await asyncio.to_thread(
                        sidecar.write_text,
                        json.dumps(
                            {
                                "url": url,
                                "size": total,
                                "etag": response.headers.get("etag"),
                                "last-modified": response.headers.get("last-modified"),
                            }
                        ),
                        encoding="utf-8",
                    )

        if expected_total is not None and total != expected_total:
            raise ArtifactIntegrityError(
                f"Incomplete download for {url}: received {total} of {expected_total} bytes"
            )
        sha256 = digest.hexdigest()
        if expected_sha256 and sha256.lower() != expected_sha256.lower():
            raise ArtifactIntegrityError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {sha256}"
            )
        partial.replace(destination)
        sidecar.unlink(missing_ok=True)
        return destination, sha256, total, headers

    @asynccontextmanager
    async def _stream_response(
        self, url: str, headers: dict[str, str]
    ) -> AsyncIterator[httpx.Response]:
        retryable = {429, 500, 502, 503, 504}
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(self.settings.sec_max_retries + 1):
                current = url
                response: httpx.Response | None = None
                yielded = False
                try:
                    for _ in range(10):
                        self._validate_url(current)
                        await self.rate_limiter.acquire()
                        response = await self.client.send(
                            self.client.build_request("GET", current, headers=headers),
                            stream=True,
                            follow_redirects=False,
                        )
                        SEC_REQUESTS.labels(
                            host=urlparse(current).hostname or "unknown",
                            status=str(response.status_code),
                        ).inc()
                        if not response.is_redirect:
                            break
                        redirected = urljoin(current, response.headers["location"])
                        self._validate_url(redirected)
                        await response.aclose()
                        current = redirected
                    if response is None or response.is_redirect:
                        raise SecAccessError(f"Too many SEC redirects: {url}")
                    self._validate_url(str(response.url))
                    if response.status_code not in retryable:
                        if response.status_code >= 400 and response.status_code != 416:
                            try:
                                response.raise_for_status()
                            except httpx.HTTPStatusError as exc:
                                raise SecAccessError(
                                    f"SEC request failed permanently: {current}: {exc}"
                                ) from exc
                        try:
                            yielded = True
                            yield response
                        finally:
                            await response.aclose()
                        return
                    last_error = SecAccessError(
                        f"Retryable SEC response {response.status_code}: {current}"
                    )
                    await response.aclose()
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if yielded:
                        raise
                    SEC_REQUESTS.labels(
                        host=urlparse(current).hostname or "unknown", status="error"
                    ).inc()
                    last_error = exc
                    if response is not None:
                        await response.aclose()
                if attempt < self.settings.sec_max_retries:
                    await asyncio.sleep(
                        _retry_delay(
                            response.headers.get("Retry-After") if response is not None else None,
                            attempt,
                        )
                    )
            raise SecAccessError(f"SEC streaming request exhausted retries: {url}: {last_error}")


def _download_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"etag", "last-modified", "content-type", "content-length"}
    }


def _observe_response(method: str, url: str, response: httpx.Response) -> None:
    host = urlparse(url).hostname or "unknown"
    SEC_REQUESTS.labels(host=host, status=str(response.status_code)).inc()
    if method.upper() == "GET":
        SOURCE_BYTES.labels(source=host, dataset="sec").inc(len(response.content))


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    import re

    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value or "")
    if not match:
        return None
    start, end, total = (int(part) for part in match.groups())
    return (start, end, total) if start <= end < total else None


def _content_range_total(value: str | None) -> int | None:
    import re

    match = re.fullmatch(r"bytes \*/(\d+)", value or "")
    return int(match.group(1)) if match else None


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def build_archive_directory_url(cik: int, accession: str) -> str:
    accession_digits = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_digits}"


def _retry_delay(header: str | None, attempt: int) -> float:
    delay = min(60.0, 2**attempt + random.random())
    if header:
        try:
            if header.isdigit():
                if len(header) > 12 or int(header) > 86400 * 365 * 1000:
                    raise SecAccessError(
                        "Retry-After exceeds scheduling horizon; operator review required"
                    )
                delay = float(header)
            else:
                delay = max(
                    0.0, (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
                )
        except (ValueError, OverflowError, TypeError):
            pass
    if delay > 60:
        raise SecRetryDeferred(datetime.now(UTC) + timedelta(seconds=delay))
    return float(delay)
