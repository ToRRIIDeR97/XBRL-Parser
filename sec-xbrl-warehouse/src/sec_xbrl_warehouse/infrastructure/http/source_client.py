"""Rate-limited, host-allowlisted HTTP client for one source (regulator).

`SourceHttpClient` enforces a `SourcePolicy`: only the policy's hosts over HTTPS
(every redirect hop is validated before it is requested), the policy's headers,
rate and concurrency, the configured byte limits and retries, and the source's
egress lock, taken once before the first allowed request. `SecHttpClient` is this
client with the SEC policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin, urlparse

import aiofiles
import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.sources import SEC_SOURCE
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    SourceAccessError,
    SourceRetryDeferred,
)
from sec_xbrl_warehouse.infrastructure.http.egress_lock import EgressLock
from sec_xbrl_warehouse.infrastructure.http.rate_limiter import AsyncRateLimiter
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.observability.metrics import SEC_REQUESTS, SOURCE_BYTES
from sec_xbrl_warehouse.sources.base import SourcePolicy, redact_params

logger = get_logger(__name__)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceHttpClient:
    """Rate-limited HTTP client restricted to one source's hosts."""

    # Raised for refused or failed requests; `SecHttpClient` raises `SecAccessError`.
    error: type[SourceAccessError] = SourceAccessError

    def __init__(
        self,
        settings: Settings,
        policy: SourcePolicy,
        client: httpx.AsyncClient | None = None,
        egress_lock: EgressLock | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        # Messages and log events name the source ("SEC" for the SEC client).
        self.label = "SEC" if policy.code == SEC_SOURCE else policy.code
        # Taken once, right before the first request that passes URL validation;
        # released on exit. None keeps the client lock-free (tests).
        self.egress_lock = egress_lock
        self._egress_held = False
        self._egress_guard = asyncio.Lock()
        self.allowed_hosts = frozenset(policy.allowed_hosts)
        if policy.secret_params:
            _redact_library_logs(policy.secret_params)
        self.headers = dict(policy.headers)
        self.rate_limiter = AsyncRateLimiter(policy.max_requests_per_second)
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)
        timeout = httpx.Timeout(
            connect=settings.sec_connect_timeout_seconds,
            read=settings.sec_read_timeout_seconds,
            write=settings.sec_read_timeout_seconds,
            pool=settings.sec_connect_timeout_seconds,
        )
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=self.headers
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self._owns_client:
                await self.client.aclose()
        finally:
            if self.egress_lock is not None and self._egress_held:
                self._egress_held = False
                await self.egress_lock.release()

    async def ensure_egress(self) -> None:
        """Hold the egress lock before the first request (no-op without a lock)."""
        if self.egress_lock is None:
            return
        if self._egress_held:
            verify = getattr(self.egress_lock, "verify", None)
            if verify is not None:
                await verify()
            return
        async with self._egress_guard:
            if not self._egress_held:
                await self.egress_lock.acquire()
                self._egress_held = True

    def _check_identity(self) -> None:
        """Refuse to send before the source's identity is configured (SEC: user agent)."""

    def _deferred(self, retry_at: datetime) -> SourceRetryDeferred:
        return SourceRetryDeferred(retry_at, self.label)

    def _show(self, text: object) -> str:
        """`text` with the policy's credential parameters masked (logs, errors, files)."""
        return self.policy.redact(str(text))

    def _scrub(self, exc: BaseException) -> BaseException:
        """Mask credentials in a library exception before it is chained or logged."""
        if self.policy.secret_params:
            exc.args = tuple(self._show(arg) if isinstance(arg, str) else arg for arg in exc.args)
        return exc

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
            if parsed.hostname not in self.allowed_hosts:
                raise self.error(
                    f"Refusing non-{self.label} host through {type(self).__name__}: "
                    f"{parsed.hostname!r}"
                )
            if (
                parsed.scheme != "https"
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise self.error(f"{self.label} URLs must use HTTPS port 443 without credentials")
        except ValueError as exc:
            raise self.error(f"Invalid {self.label} URL") from exc

    async def _buffered_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # The policy's headers go on every request, so an injected client sends them too.
        kwargs["headers"] = {**self.headers, **(kwargs.get("headers") or {})}
        async with self.client.stream(method, url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > self.settings.response_max_bytes:
                    raise ArtifactIntegrityError(
                        f"{self.label} response exceeds configured byte limit"
                    )
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
        self._check_identity()
        self._validate_url(url)
        await self.ensure_egress()
        kwargs["follow_redirects"] = False
        last_error: Exception | None = None

        for attempt in range(self.settings.sec_max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self._buffered_request(method, url, **kwargs)
                _observe_response(method, url, response, self.policy.code)
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
                    _observe_response(method, url, response, self.policy.code)
                if response.is_redirect:
                    raise self.error(f"Too many {self.label} redirects: {self._show(url)}")
                self._validate_url(str(response.url))
                if response.status_code not in _RETRYABLE_STATUSES:
                    response.raise_for_status()
                    return response

                await response.aclose()
                delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                logger.warning(
                    f"{self.policy.code}_request_retry",
                    url=self._show(url),
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                )
                last_error = self.error(
                    f"Retryable {self.label} response {response.status_code}: {self._show(url)}"
                )
                await response.aclose()
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                self._scrub(exc)
                if not isinstance(exc, httpx.HTTPStatusError):
                    SEC_REQUESTS.labels(
                        host=urlparse(url).hostname or "unknown", status="error"
                    ).inc()
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status not in _RETRYABLE_STATUSES:
                        raise self.error(
                            f"{self.label} request failed permanently: "
                            f"{self._show(url)}: {self._show(exc)}"
                        ) from exc
                if attempt >= self.settings.sec_max_retries:
                    break
                delay = min(60.0, (2**attempt) + random.random())
                logger.warning(
                    f"{self.policy.code}_request_exception_retry",
                    url=self._show(url),
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    error=self._show(exc),
                )
                await asyncio.sleep(delay)

        raise self.error(
            f"{self.label} request exhausted retries: {self._show(url)}: {self._show(last_error)}"
        )

    async def get_text(self, url: str) -> str:
        response = await self._request("GET", url)
        return response.text

    async def get_json(self, url: str) -> dict[str, Any]:
        response = await self._request("GET", url)
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise self.error(f"{self.label} returned invalid JSON for {self._show(url)}") from exc
        if not isinstance(value, dict):
            raise self.error(f"Expected a JSON object from {self._show(url)}")
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
        self._check_identity()
        await self.ensure_egress()
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
        # The sidecar records the URL with credential parameters masked.
        shown = self._show(url)
        try:
            reusable_partial = (
                prior.get("url") == shown and int(prior.get("size", -1)) == partial.stat().st_size
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
                            f"SHA-256 mismatch for complete partial {shown}: {sha256}"
                        )
                    partial.replace(destination)
                    sidecar.unlink(missing_ok=True)
                    return destination, sha256, total, _download_headers(response)
                partial.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                raise ArtifactIntegrityError(
                    f"{self.label} rejected an invalid byte range for {shown}"
                )
            else:
                response.raise_for_status()
                content_range = _content_range(response.headers.get("content-range"))
                if (
                    response.status_code != 206
                    or not content_range
                    or content_range[0] != resume_from
                ):
                    raise ArtifactIntegrityError(f"Malformed resume response for {shown}")
                start, end, expected_total = content_range
                content_length = _positive_int(response.headers.get("content-length"))
                if content_length is not None and content_length != end - start + 1:
                    raise ArtifactIntegrityError(
                        f"Malformed Content-Length for resumed download: {shown}"
                    )
                for name in ("etag", "last-modified"):
                    if prior.get(name) and response.headers.get(name) != prior[name]:
                        raise ArtifactIntegrityError(
                            f"{name} changed during resumed download: {shown}"
                        )
            if expected_total is not None and expected_total > byte_limit:
                raise ArtifactIntegrityError("Download exceeds configured byte limit")
            headers = _download_headers(response)
            mode = "ab" if resume_from > 0 and response.status_code == 206 else "wb"
            await asyncio.to_thread(
                sidecar.write_text,
                json.dumps(
                    {
                        "url": shown,
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
                        source=urlparse(url).hostname or "unknown", dataset=self.policy.code
                    ).inc(len(chunk))
                    await asyncio.to_thread(
                        sidecar.write_text,
                        json.dumps(
                            {
                                "url": shown,
                                "size": total,
                                "etag": response.headers.get("etag"),
                                "last-modified": response.headers.get("last-modified"),
                            }
                        ),
                        encoding="utf-8",
                    )

        if expected_total is not None and total != expected_total:
            raise ArtifactIntegrityError(
                f"Incomplete download for {shown}: received {total} of {expected_total} bytes"
            )
        sha256 = digest.hexdigest()
        if expected_sha256 and sha256.lower() != expected_sha256.lower():
            raise ArtifactIntegrityError(
                f"SHA-256 mismatch for {shown}: expected {expected_sha256}, got {sha256}"
            )
        partial.replace(destination)
        sidecar.unlink(missing_ok=True)
        return destination, sha256, total, headers

    @asynccontextmanager
    async def _stream_response(
        self, url: str, headers: dict[str, str]
    ) -> AsyncIterator[httpx.Response]:
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
                            self.client.build_request(
                                "GET", current, headers={**self.headers, **headers}
                            ),
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
                        raise self.error(f"Too many {self.label} redirects: {self._show(url)}")
                    self._validate_url(str(response.url))
                    if response.status_code not in _RETRYABLE_STATUSES:
                        if response.status_code >= 400 and response.status_code != 416:
                            try:
                                response.raise_for_status()
                            except httpx.HTTPStatusError as exc:
                                self._scrub(exc)
                                raise self.error(
                                    f"{self.label} request failed permanently: "
                                    f"{self._show(current)}: {self._show(exc)}"
                                ) from exc
                        try:
                            yielded = True
                            yield response
                        finally:
                            await response.aclose()
                        return
                    last_error = self.error(
                        f"Retryable {self.label} response {response.status_code}: "
                        f"{self._show(current)}"
                    )
                    await response.aclose()
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    self._scrub(exc)
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
                        self._retry_delay(
                            response.headers.get("Retry-After") if response is not None else None,
                            attempt,
                        )
                    )
            raise self.error(
                f"{self.label} streaming request exhausted retries: {self._show(url)}: "
                f"{self._show(last_error)}"
            )

    def _retry_delay(self, header: str | None, attempt: int) -> float:
        """Backoff with jitter, or the server's `Retry-After` (seconds or HTTP date).

        A delay over a minute releases the worker (`SourceRetryDeferred`) instead
        of sleeping; an absurd `Retry-After` is refused for operator review.
        """
        delay = min(60.0, 2**attempt + random.random())
        if header:
            try:
                if header.isdigit():
                    if len(header) > 12 or int(header) > 86400 * 365 * 1000:
                        raise self.error(
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
            raise self._deferred(datetime.now(UTC) + timedelta(seconds=delay))
        return float(delay)


_LOG_SECRET_PARAMS: set[str] = set()


class _RedactSecretParams(logging.Filter):
    """Masks credential query parameters in httpx/httpcore log records (request URLs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _LOG_SECRET_PARAMS:
            message = record.getMessage()
            redacted = redact_params(message, _LOG_SECRET_PARAMS)
            if redacted != message:
                record.msg, record.args = redacted, None
        return True


_LOG_FILTER = _RedactSecretParams()


def _redact_library_logs(names: frozenset[str]) -> None:
    """Install the redaction filter on the HTTP libraries' loggers (idempotent)."""
    _LOG_SECRET_PARAMS.update(names)
    for name in ("httpx", "httpcore"):
        library = logging.getLogger(name)
        if _LOG_FILTER not in library.filters:
            library.addFilter(_LOG_FILTER)


def _download_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"etag", "last-modified", "content-type", "content-length"}
    }


def _observe_response(method: str, url: str, response: httpx.Response, dataset: str) -> None:
    host = urlparse(url).hostname or "unknown"
    SEC_REQUESTS.labels(host=host, status=str(response.status_code)).inc()
    if method.upper() == "GET":
        SOURCE_BYTES.labels(source=host, dataset=dataset).inc(len(response.content))


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value or "")
    if not match:
        return None
    start, end, total = (int(part) for part in match.groups())
    return (start, end, total) if start <= end < total else None


def _content_range_total(value: str | None) -> int | None:
    match = re.fullmatch(r"bytes \*/(\d+)", value or "")
    return int(match.group(1)) if match else None


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0 else None
