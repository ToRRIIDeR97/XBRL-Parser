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
