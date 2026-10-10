"""Environment-based settings.

Values arrive from Nomad's Vault template (consul-template). A key that is missing in Vault
renders as the literal string ``<no value>``, which is truthy: every value read here goes
through ``_secret()``, which treats empty, whitespace-only and ``<no value>`` as missing
(faalpatroon 2).

Missing values never crash startup: the service starts *degraded* (``/health`` stays 200,
``vonk_degraded`` = 1) so that dgoss can run without Vault. ``vonk poll`` checks
``missing_secrets("poll")`` itself and exits non-zero.

Secret values and child names are excluded from ``repr`` and from every error message.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from vonk.models import Tak

Role = Literal["serve", "poll"]

NO_VALUE = "<no value>"

DEFAULT_DB_PATH = "vonk.db"

# Env var names required per role. CHILD_NAMES / TAKKEN count as missing when they parse to
# an empty or invalid value.
_REQUIRED: Mapping[Role, tuple[str, ...]] = {
    "serve": (
        "BAIKAL_URL",
        "BAIKAL_USER",
        "BAIKAL_PASSWORD",
        "BAIKAL_CALENDAR",
        "GOTIFY_URL",
        "GOTIFY_TOKEN",
        "VONK_API_TOKEN",
        "VONK_PUBLIC_URL",
        "CHILD_NAMES",
    ),
    "poll": (
        "ANTHROPIC_API_KEY",
        "VONK_API_TOKEN",
        "VONK_API_URL",
        "TAKKEN",
    ),
}


def _secret(value: str | None) -> str | None:
    """Normalise a raw env value: None for empty, whitespace-only or ``<no value>``."""
    if value is None:
        return None
    value = value.strip()
    if not value or value == NO_VALUE:
        return None
    return value


def _parse_child_names(raw: str | None, errors: list[str]) -> dict[Tak, str]:
    """Parse CHILD_NAMES, a JSON object ``{"<tak>": "<name>"}``. Never echoes the value."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        errors.append("CHILD_NAMES is not valid JSON")
        return {}
    if not isinstance(data, dict):
        errors.append("CHILD_NAMES must be a JSON object of tak -> name")
        return {}
    names: dict[Tak, str] = {}
    for key, name in data.items():
        try:
            tak = Tak(str(key).strip().lower())
        except ValueError:
            errors.append(f"CHILD_NAMES has an unknown tak {key!r}")
            continue
        clean = _secret(name) if isinstance(name, str) else None
        if clean is None:
            errors.append(f"CHILD_NAMES has an empty name for tak {tak.value!r}")
            continue
        names[tak] = clean
    return names


def _parse_takken(raw: str | None, errors: list[str]) -> tuple[Tak, ...]:
    """Parse TAKKEN: comma/whitespace-separated (``kabouters,jeevees``) or a JSON list."""
    if raw is None:
        return ()
    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            errors.append("TAKKEN is not valid JSON")
            return ()
        if not isinstance(items, list):
            errors.append("TAKKEN must be a list")
            return ()
        parts = [str(i) for i in items]
    else:
        parts = re.split(r"[,\s]+", raw)
    takken: list[Tak] = []
    for part in (p.strip().lower() for p in parts):
        if not part:
            continue
        try:
            tak = Tak(part)
        except ValueError:
            errors.append(f"TAKKEN has an unknown tak {part!r}")
            continue
        if tak not in takken:
            takken.append(tak)
    return tuple(takken)


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime configuration. Build with ``Settings.from_env()``."""

    baikal_url: str | None = None
    baikal_user: str | None = None
    baikal_password: str | None = field(default=None, repr=False)
    baikal_calendar: str | None = None
    gotify_url: str | None = None
    gotify_token: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    vonk_api_token: str | None = field(default=None, repr=False)
    vonk_api_url: str | None = None
    vonk_db_path: str = DEFAULT_DB_PATH
    vonk_public_url: str | None = None
    child_names: Mapping[Tak, str] = field(default_factory=dict, repr=False)
    """Tak -> child name. Personal data: only from Vault, never logged."""
    takken: tuple[Tak, ...] = ()
    """Takken to process (besides the group-wide jaarvonk)."""
    errors: tuple[str, ...] = ()
    """Parse errors of structured values (messages never contain the values)."""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from ``env`` (default ``os.environ``)."""
        env = os.environ if env is None else env
        errors: list[str] = []

        def get(name: str) -> str | None:
            return _secret(env.get(name))

        return cls(
            baikal_url=get("BAIKAL_URL"),
            baikal_user=get("BAIKAL_USER"),
            baikal_password=get("BAIKAL_PASSWORD"),
            baikal_calendar=get("BAIKAL_CALENDAR"),
            gotify_url=get("GOTIFY_URL"),
            gotify_token=get("GOTIFY_TOKEN"),
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            vonk_api_token=get("VONK_API_TOKEN"),
            vonk_api_url=get("VONK_API_URL"),
            vonk_db_path=get("VONK_DB_PATH") or DEFAULT_DB_PATH,
            vonk_public_url=get("VONK_PUBLIC_URL"),
            child_names=_parse_child_names(get("CHILD_NAMES"), errors),
            takken=_parse_takken(get("TAKKEN"), errors),
            errors=tuple(errors),
        )

    def _has(self, name: str) -> bool:
        if name == "CHILD_NAMES":
            return bool(self.child_names)
        if name == "TAKKEN":
            return bool(self.takken)
        return getattr(self, name.lower()) is not None

    def missing_secrets(self, role: Role) -> list[str]:
        """Names of the env vars ``role`` needs that are missing (or ``<no value>``)."""
        if role not in _REQUIRED:
            raise ValueError(f"unknown role {role!r}")
        return [name for name in _REQUIRED[role] if not self._has(name)]

    @property
    def degraded(self) -> bool:
        """True when the service (role ``serve``) lacks a required value or has a parse
        error; exported as ``vonk_degraded``."""
        return bool(self.missing_secrets("serve") or self.errors)
