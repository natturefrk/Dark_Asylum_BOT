"""RuneScape 3 data provider interface and provider selection.

The real implementation lives in ``rs3api`` (public Jagex endpoints, no API key).
``UnavailableProvider`` remains as an explicit "off switch" (``RS3_API_DISABLED=1``)
and as the safe default for tests. Nothing in this project fabricates data.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderUnavailable(RuntimeError):
    """The data source is disabled, down, rate-limited or returned something unparseable."""


class PlayerNotFound(ValueError):
    """The source answered normally but has no such player/clan (a lookup miss, not an outage)."""


@dataclass(frozen=True)
class ClanMate:
    """One row of the public clan roster."""

    rsn: str
    rank: str
    total_xp: int
    kills: int


@dataclass(frozen=True)
class Activity:
    """One RuneMetrics adventure-log entry (date string is kept exactly as served)."""

    date: str
    text: str
    details: str


@dataclass(frozen=True)
class Profile:
    """RuneMetrics profile result: either private (no data) or a list of recent activities."""

    private: bool
    activities: list[Activity]


class XpProvider(ABC):
    """Contract for anything that can look up RS3 data. Methods raise ``ProviderUnavailable`` when down."""

    @abstractmethod
    async def fetch_skills(self, rsn: str) -> dict[str, int]:
        """Return ``{skill_name: xp}`` for a player (without the Overall total)."""

    async def fetch_player(self, rsn: str) -> dict[str, int]:
        """Skills plus an ``Overall`` total; the default derives it by summing the skills."""
        skills = await self.fetch_skills(rsn)
        return {**skills, "Overall": sum(skills.values())}

    async def fetch_roster(self, clan_name: str) -> list[ClanMate]:
        """Return the clan roster; providers without roster support are unavailable for it."""
        raise ProviderUnavailable("This provider cannot read clan rosters.")

    async def fetch_profile(self, rsn: str) -> Profile:
        """Return the RuneMetrics profile; providers without it are unavailable."""
        raise ProviderUnavailable("This provider cannot read adventure logs.")


class UnavailableProvider(XpProvider):
    """Provider that always reports unavailable (used when the API is switched off)."""

    async def fetch_skills(self, rsn: str) -> dict[str, int]:
        """Always raise: no data source is active."""
        raise ProviderUnavailable("The RuneScape data source is switched off (RS3_API_DISABLED).")


_provider: XpProvider | None = None


def set_provider(provider: XpProvider | None) -> None:
    """Override the active provider (tests, or ``None`` to reset to the default)."""
    global _provider
    _provider = provider


def get_provider() -> XpProvider:
    """Return the active provider: the real HTTP one unless explicitly disabled."""
    global _provider
    if _provider is None:
        if os.environ.get("RS3_API_DISABLED", "").lower() in ("1", "true", "yes"):
            _provider = UnavailableProvider()
        else:
            from .rs3api import Rs3Provider  # imported lazily: it depends on this module

            _provider = Rs3Provider()
    return _provider
