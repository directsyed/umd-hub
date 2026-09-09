"""Config loader: YAML + secrets.env, validated with pydantic. One cached load.

Same shape as Hardware Parser's core/config.py: config.yaml holds every tunable,
secrets.env holds credentials and is loaded into the environment (never into the
Config object), and load_config() is lru_cached so every module sees one instance.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"
DEFAULT_SECRETS = REPO_ROOT / "secrets.env"


class SemesterCfg(BaseModel):
    name: str = "Fall 2026"
    year: int = 2026
    start: str  # YYYY-MM-DD
    end: str    # YYYY-MM-DD
    timezone: str = "America/New_York"


class PathsCfg(BaseModel):
    db: str = "state.sqlite"
    seed: str = "seed/fall-2026.yaml"
    lock: str = "state/refresh.lock"
    study_repo: str
    backlog_file: str = "Claude Master/deferred-work-backlog.md"


class WebCfg(BaseModel):
    bind: str = "0.0.0.0"
    port: int = 8765
    auth_mode: str = "lan"  # lan | both
    allowed_cidrs: list[str] = Field(
        default_factory=lambda: ["10.0.0.0/24", "10.10.0.0/24", "127.0.0.1/32", "::1/128"]
    )
    cookie_days: int = 90
    stale_after_hours: int = 14
    refresh_unit: str = "umdhub-refresh.service"


class CourseCfg(BaseModel):
    name: str
    color: str = "#7aa2f7"
    canvas_names: list[str] = Field(default_factory=list)
    email_senders: list[str] = Field(default_factory=list)
    subject_tokens: list[str] = Field(default_factory=list)
    gradescope_short: list[str] = Field(default_factory=list)
    piazza_names: list[str] = Field(default_factory=list)


class SourceCfg(BaseModel):
    """Per-source block. Unknown keys are kept (each adapter reads its own)."""
    model_config = ConfigDict(extra="allow")
    enabled: bool = True

    def get(self, key: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(key, default)


class ExtractCfg(BaseModel):
    enabled: bool = True
    claude_bin: str = "claude"
    model: str = "haiku"
    # low | medium | high — measured 2026-09-09 on a 5-post Piazza batch: default ≈ 12.2k output
    # tokens / 121 s, low ≈ 9.2k / 84 s, medium ≈ 8.8k / 90 s. Thinking fully off breaks the
    # structured-output schema, so "low" is the floor. Empty string = CLI default.
    effort: str = "low"
    batch_size: int = 5
    max_batch_chars: int = 9000
    max_body_chars: int = 2000
    min_body_chars: int = 40
    max_batches_per_run: int = 6
    timeout_s: int = 240
    time_box_s: int = 660
    retry_failed_after_hours: int = 12
    budget_usd: float = 0.25
    sources: list[str] = Field(
        default_factory=lambda: ["email", "piazza", "canvas_announce", "site_diff"]
    )


class NotifyCfg(BaseModel):
    enabled: bool = True
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"
    username: str = "umd-hub"
    due_soon_hours: int = 24
    backlog_alert_days: int = 8
    credential_stale_days: int = 3


class Config(BaseModel):
    semester: SemesterCfg
    paths: PathsCfg
    web: WebCfg = Field(default_factory=WebCfg)
    courses: dict[str, CourseCfg]
    sources: dict[str, SourceCfg] = Field(default_factory=dict)
    extract: ExtractCfg = Field(default_factory=ExtractCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)

    # ---- path helpers (relative paths resolve against the repo root) ----
    def _abs(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else REPO_ROOT / path

    def db_path(self) -> Path:
        return self._abs(self.paths.db)

    def seed_path(self) -> Path:
        return self._abs(self.paths.seed)

    def lock_path(self) -> Path:
        return self._abs(self.paths.lock)

    def backlog_path(self) -> Path:
        return Path(self.paths.study_repo) / self.paths.backlog_file

    def course_codes(self) -> list[str]:
        return list(self.courses.keys())

    def source(self, name: str) -> SourceCfg:
        return self.sources.get(name, SourceCfg(enabled=False))


def _load_secrets(path: Path) -> None:
    if path.exists():
        load_dotenv(path, override=False)


@lru_cache(maxsize=4)
def load_config(config_path: str | None = None, secrets_path: str | None = None) -> Config:
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    sec_path = Path(secrets_path) if secrets_path else DEFAULT_SECRETS
    _load_secrets(sec_path)
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)


def env(name: str, *, required: bool = False, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"required env var {name!r} is unset")
    return val or None
