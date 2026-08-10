from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ENV_KEYS = {
    "SPRING_DATASOURCE_URL",
    "SPRING_DATASOURCE_USERNAME",
    "SPRING_DATASOURCE_PASSWORD",
    "LOCAL_POSTGRES_HOST",
    "LOCAL_POSTGRES_PORT",
    "LOCAL_POSTGRES_DB",
    "LOCAL_POSTGRES_USER",
    "LOCAL_POSTGRES_PASSWORD",
    "ODSAY_ENABLED",
    "ODSAY_BASE_URL",
    "ODSAY_API_KEY",
    "TMAP_WALKING_ENABLED",
    "TMAP_BASE_URL",
    "SKT_API_KEY",
}


def load_runtime_env() -> None:
    base_dir = Path(__file__).resolve().parent
    candidates = [
        base_dir.parent / ".env",
        base_dir.parent.parent / "server" / ".env",
        base_dir.parent.parent / "server" / ".env.example",
    ]
    for path in candidates:
        load_env_file(path)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in RUNTIME_ENV_KEYS:
            continue
        value = value.strip().strip('"').strip("'")
        if not value or key in os.environ:
            continue
        os.environ[key] = value
