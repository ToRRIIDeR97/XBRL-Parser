$ErrorActionPreference = "Stop"
$Python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "py" }
$Venv = if ($env:VENV) { $env:VENV } else { ".venv" }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env. Set XBRL_SEC_USER_AGENT before any SEC network request."
}

if ($Python -eq "py") {
    & py -3.12 -m venv $Venv
} else {
    & $Python -m venv $Venv
}
& "$Venv\Scripts\python.exe" -m pip install --upgrade pip
& "$Venv\Scripts\python.exe" -m pip install -e ".[dev]"
docker compose up -d postgres clickhouse minio minio-init
& "$Venv\Scripts\alembic.exe" upgrade head
& "$Venv\Scripts\xbrl-warehouse.exe" db init-clickhouse
& "$Venv\Scripts\xbrl-warehouse.exe" doctor --skip-sec-identity
Write-Host "Bootstrap complete. Edit .env and run xbrl-warehouse doctor."
