import json
from pathlib import Path

import httpx
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SecAccessError
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        temp_dir=tmp_path,
        sec_user_agent="Test Suite test@example.com",
        sec_max_retries=0,
        sec_max_requests_per_second=10,
    )


async def test_non_sec_url_is_rejected_before_transport(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        sec = SecHttpClient(_settings(tmp_path), client)
        with pytest.raises(SecAccessError, match="non-SEC host"):
            await sec.get_text("https://example.com/private")
        assert calls == 0
    finally:
        await client.aclose()


async def test_download_resumes_only_matching_range_and_validator(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.zip"
    partial = destination.with_suffix(".zip.part")
    partial.write_bytes(b"abc")
    partial.with_suffix(".part.json").write_text(
        json.dumps({"url": "https://www.sec.gov/file", "size": 3, "etag": '"v1"'}),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=3-"
        assert request.headers["if-range"] == '"v1"'
        return httpx.Response(
            206,
            content=b"def",
            headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"v1"'},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await SecHttpClient(_settings(tmp_path), client).download(
            "https://www.sec.gov/file", destination
        )
        assert result[2] == 6
        assert destination.read_bytes() == b"abcdef"
        assert not partial.exists()
    finally:
        await client.aclose()


async def test_download_rejects_changed_resume_validator(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.zip"
    partial = destination.with_suffix(".zip.part")
    partial.write_bytes(b"abc")
    partial.with_suffix(".part.json").write_text(
        json.dumps({"url": "https://www.sec.gov/file", "size": 3, "etag": '"v1"'}),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            content=b"def",
            headers={"Content-Range": "bytes 3-5/6", "ETag": '"v2"'},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ArtifactIntegrityError, match="etag changed"):
            await SecHttpClient(_settings(tmp_path), client).download(
                "https://www.sec.gov/file", destination
            )
    finally:
        await client.aclose()


async def test_download_accepts_verified_complete_partial_on_416(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.zip"
    partial = destination.with_suffix(".zip.part")
    partial.write_bytes(b"abcdef")
    partial.with_suffix(".part.json").write_text(
        json.dumps({"url": "https://www.sec.gov/file", "size": 6, "etag": '"v1"'}),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            416,
            headers={"Content-Range": "bytes */6", "ETag": '"v1"'},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await SecHttpClient(_settings(tmp_path), client).download(
            "https://www.sec.gov/file", destination
        )
        assert result[2] == 6
        assert destination.read_bytes() == b"abcdef"
    finally:
        await client.aclose()


async def test_download_keeps_incomplete_body_for_safe_resume(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.zip"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"abc",
            headers={"Content-Length": "6", "ETag": '"v1"'},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ArtifactIntegrityError, match="Incomplete download"):
            await SecHttpClient(_settings(tmp_path), client).download(
                "https://www.sec.gov/file", destination
            )
        assert not destination.exists()
        assert destination.with_suffix(".zip.part").read_bytes() == b"abc"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/file",
        "https://www.sec.gov:8443/file",
        "https://name:secret@www.sec.gov/file",
        "https://www.sec.gov:invalid/file",
    ],
)
async def test_unsafe_sec_urls_never_reach_transport(tmp_path, url):
    def transport(request):
        pytest.fail("Unsafe URL reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sec = SecHttpClient(_settings(tmp_path), client)
        with pytest.raises(SecAccessError):
            await sec.get_text(url)
        with pytest.raises(SecAccessError):
            await sec.download(url, tmp_path / "source.zip")


@pytest.mark.parametrize("streaming", [False, True])
async def test_redirect_downgrades_rejected_with_redirecting_injected_client(tmp_path, streaming):
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://www.sec.gov/file"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), follow_redirects=True
    ) as client:
        sec = SecHttpClient(_settings(tmp_path), client)
        with pytest.raises(SecAccessError):
            if streaming:
                await sec.download("https://www.sec.gov/file", tmp_path / "source.zip")
            else:
                await sec.get_text("https://www.sec.gov/file")
    assert calls == ["https://www.sec.gov/file"]


async def test_download_and_decoded_response_byte_budgets(tmp_path):
    import gzip

    settings = _settings(tmp_path).model_copy(
        update={"download_max_bytes": 5, "response_max_bytes": 5}
    )

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"abc"
            yield b"def"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    ) as client:
        with pytest.raises(ArtifactIntegrityError, match="byte limit"):
            await SecHttpClient(settings, client).download(
                "https://www.sec.gov/file", tmp_path / "file"
            )
    assert not (tmp_path / "file").exists()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=gzip.compress(b"abcdef"), headers={"Content-Encoding": "gzip"}
            )
        )
    ) as client:
        with pytest.raises(ArtifactIntegrityError, match="byte limit"):
            await SecHttpClient(settings, client).get_text("https://www.sec.gov/file")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=gzip.compress(b"abc"), headers={"Content-Encoding": "gzip"}
            )
        )
    ) as client:
        assert await SecHttpClient(settings, client).get_text("https://www.sec.gov/file") == "abc"


async def test_midstream_failure_can_resume_on_next_attempt(tmp_path):
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (1024 * 1024)
            raise httpx.ReadError("interrupted")

    destination = tmp_path / "file"
    settings = _settings(tmp_path).model_copy(update={"sec_max_retries": 1})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=BrokenBody(), headers={"ETag": "v1"})
        )
    ) as client:
        with pytest.raises(httpx.ReadError):
            await SecHttpClient(settings, client).download("https://www.sec.gov/file", destination)
    assert destination.with_suffix(".part").stat().st_size == 1024 * 1024

    def resumed(request):
        assert request.headers["range"] == "bytes=1048576-"
        return httpx.Response(
            206,
            content=b"z",
            headers={"ETag": "v1", "Content-Range": "bytes 1048576-1048576/1048577"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(resumed)) as client:
        await SecHttpClient(settings, client).download("https://www.sec.gov/file", destination)
    assert destination.read_bytes().endswith(b"z")


async def test_long_retry_after_defers_without_sleep(monkeypatch):
    from datetime import UTC, datetime

    from sec_xbrl_warehouse.exceptions import SecRetryDeferred
    from sec_xbrl_warehouse.worker import is_retryable_exception

    async def forbidden_sleep(delay):
        raise AssertionError(f"unexpected sleep {delay}")

    monkeypatch.setattr(
        "sec_xbrl_warehouse.infrastructure.http.sec_client.asyncio.sleep", forbidden_sleep
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "999999999"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        http = SecHttpClient(Settings(sec_user_agent="Tests tests@example.com"), client)
        before = datetime.now(UTC)
        with pytest.raises(SecRetryDeferred) as error:
            await http.get_text("https://www.sec.gov/test")
        assert (error.value.retry_at - before).total_seconds() >= 999999999
        assert is_retryable_exception(error.value)
