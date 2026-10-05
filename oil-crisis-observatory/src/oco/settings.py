"""Paths, environment and user configuration.

Only source-specific variables listed in `ALLOWED_ENV` are ever read. Cloud/billing credentials
(AWS_*, GOOGLE_APPLICATION_CREDENTIALS, ARCGIS_* ...) are never read, even if present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"

# The ONLY environment variables the application reads.
ALLOWED_ENV = (
    "OCO_DATA_DIR",
    "OCO_CONTACT_EMAIL",
    "OCO_DISK_BUDGET_GB",
    "OCO_DOWNLOAD_BUDGET_GB",
    "EIA_API_KEY",
    "FIRMS_MAP_KEY",
    "CDSE_USERNAME",
    "CDSE_PASSWORD",
    "OCO_LOCAL_LLM_MODEL",
)


def _load_dotenv_allowed() -> dict[str, str]:
    """Read .env (if any) but keep only allowed keys; never export others into os.environ."""
    values: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        from dotenv import dotenv_values

        for k, v in dotenv_values(env_file).items():
            if k in ALLOWED_ENV and v:
                values[k] = v
    for k in ALLOWED_ENV:
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def env(name: str) -> str | None:
    if name not in ALLOWED_ENV:
        raise KeyError(f"{name} is not an allowed environment variable for this application")
    return _load_dotenv_allowed().get(name)


@dataclass(frozen=True)
class Paths:
    data: Path
    demo: bool = False

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def warehouse(self) -> Path:
        return self.data / "warehouse.duckdb"

    @property
    def snapshot(self) -> Path:
        return self.data / "snapshot.duckdb"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def jobs_db(self) -> Path:
        return self.state / "jobs.sqlite"

    @property
    def review_db(self) -> Path:
        return self.state / "review.sqlite"

    @property
    def exports(self) -> Path:
        return self.data / "exports"

    @property
    def satellite(self) -> Path:
        return self.data / "satellite"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def writer_lock(self) -> Path:
        return self.state / "writer.lock"

    @property
    def scheduler_pid(self) -> Path:
        return self.state / "scheduler.pid"

    def ensure(self) -> "Paths":
        for p in (self.data, self.raw, self.state, self.exports, self.satellite, self.reports):
            p.mkdir(parents=True, exist_ok=True)
        if self.demo:
            (self.data / "DEMO_SYNTHETIC_DATA_NOT_LIVE.txt").write_text(
                "This directory contains SYNTHETIC demonstration fixtures. Nothing here is live data.\n"
            )
        return self


def get_paths(demo: bool = False) -> Paths:
    base = env("OCO_DATA_DIR")
    root = Path(base) if base else PROJECT_ROOT / "data"
    if demo:
        root = root.parent / "data_demo"
    return Paths(root.resolve(), demo=demo).ensure()


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources_config() -> dict:
    return load_yaml("sources.yaml")


def topics_config() -> dict:
    return load_yaml("topics.yaml")


@dataclass
class Budgets:
    disk_gb: float = 20.0
    download_gb: float = 8.0
    extras: dict = field(default_factory=dict)


def budgets() -> Budgets:
    cfg = sources_config().get("budgets", {})
    disk = float(env("OCO_DISK_BUDGET_GB") or cfg.get("disk_gb", 20))
    dl = float(env("OCO_DOWNLOAD_BUDGET_GB") or cfg.get("satellite_download_gb", 8))
    return Budgets(disk_gb=disk, download_gb=dl, extras=cfg)


def contact() -> str:
    return env("OCO_CONTACT_EMAIL") or "unset-contact"
