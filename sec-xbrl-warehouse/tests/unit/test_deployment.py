import os
import subprocess
from pathlib import Path

import pytest
import yaml


def test_container_connections_use_internal_ports():
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    for name in ("api", "worker"):
        assert services[name]["environment"]["XBRL_CLICKHOUSE_PORT"] == 8123
    assert services["api"]["environment"]["XBRL_API_PORT"] == 8080


@pytest.mark.parametrize("fail_step", ["", "alias", "mb", "anonymous"])
def test_bucket_setup_quotes_credentials_and_fails_closed(tmp_path, fail_step):
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    entrypoint = services["minio-init"]["entrypoint"]
    marker = tmp_path / "injected"
    password = f"password with spaces; touch {marker}; $(touch {marker})"
    # Exercise the actual Compose shell script, substituting a local mc function.
    script = """mc() {
        printf '%s\\n' "$@" >> "$CALLS"
        [ "$1" != "$FAIL_STEP" ]
    }
    """ + entrypoint[2].replace("$$", "$")
    calls = tmp_path / "calls"
    result = subprocess.run(
        [entrypoint[0], entrypoint[1], script],
        env={
            **os.environ,
            "CALLS": str(calls),
            "FAIL_STEP": fail_step,
            "MINIO_ROOT_USER": "test user",
            "MINIO_ROOT_PASSWORD": password,
            "XBRL_OBJECT_STORE_BUCKET": "test-bucket",
        },
        capture_output=True,
        text=True,
    )
    args = calls.read_text().splitlines()
    assert args[:6] == ["alias", "set", "local", "http://minio:9000", "test user", password]
    assert not marker.exists()
    assert (result.returncode != 0) == bool(fail_step)
    if fail_step == "alias":
        assert "mb" not in args
    if fail_step in {"alias", "mb"}:
        assert "anonymous" not in args
    for name in ("api", "worker"):
        assert (
            services[name]["depends_on"]["minio-init"]["condition"]
            == "service_completed_successfully"
        )


def test_api_environment_excludes_writer_and_object_store_secrets():
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    api = services["api"]
    assert "env_file" not in api
    assert "XBRL_OBJECT_STORE_ENDPOINT" not in api["environment"]
    assert "POSTGRES_PASSWORD" not in api["environment"]["XBRL_POSTGRES_DSN"]
    assert "XBRL_CLICKHOUSE_PASSWORD" not in api["environment"]
    assert api["logging"]["options"]["max-file"] == "3"
