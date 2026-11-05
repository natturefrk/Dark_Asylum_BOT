"""HTTP provider for the public RuneScape 3 endpoints (no API key needed).

Endpoints used:
  * clan roster   https://secure.runescape.com/m=clan-hiscores/members_lite.ws?clanName=<name>  (CSV)
  * hiscores      https://secure.runescape.com/m=hiscore/index_lite.json?player=<rsn>            (JSON)
  * adventure log https://apps.runescape.com/runemetrics/profile/profile?user=<rsn>&activities=20 (JSON)

Everything network-related is isolated in ``HttpFetcher`` (polite spacing, timeout, backoff,
User-Agent) while the ``parse_*`` functions are pure so they can be tested with canned bytes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time

import aiohttp

from .xp import Activity, ClanMate, PlayerNotFound, Profile, ProviderUnavailable, XpProvider

USER_AGENT = "DarkAsylumClanBot/1.0 (self-hosted clan tool; polite low-rate polling)"
CLAN_URL = "https://secure.runescape.com/m=clan-hiscores/members_lite.ws"
HISCORE_URL = "https://secure.runescape.com/m=hiscore/index_lite.json"
PROFILE_URL = "https://apps.runescape.com/runemetrics/profile/profile"


def norm_rsn(name: str) -> str:
    """Canonical comparison key for an RSN: case-insensitive, with space, underscore and nbsp equal."""
    return name.replace("\xa0", " ").replace("_", " ").strip().lower()


class HttpFetcher:
    """Rate-limited GET client with a timeout and exponential backoff.

    Requests are serialised and spaced at least ``min_interval`` seconds apart so a
    whole-clan sync stays gentle on Jagex's servers. 429/5xx/network failures are
    retried with growing delays; after the last attempt ``ProviderUnavailable`` is raised.
    """

    def __init__(self, min_interval: float = 1.2, timeout: float = 15.0, retries: int = 3):
        """Configure spacing, per-request timeout and the number of attempts."""
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _sleep(self, seconds: float) -> None:
        """Sleep helper (a separate method so tests can make it instant)."""
        await asyncio.sleep(seconds)

    async def _once(self, url: str, params: dict[str, str]) -> tuple[int, bytes, str | None]:
        """Perform one real HTTP GET; returns (status, body, Retry-After header)."""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, params=params) as resp:
                return resp.status, await resp.read(), resp.headers.get("Retry-After")

    async def get(self, url: str, params: dict[str, str]) -> tuple[int, bytes]:
        """GET with spacing and retries. 200 and 404 are returned; everything else retries then fails."""
        last_problem = "no attempt made"
        for attempt in range(self.retries):
            async with self._lock:  # keep global spacing between any two requests
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await self._sleep(wait)
                try:
                    status, body, retry_after = await self._once(url, params)
                except (TimeoutError, aiohttp.ClientError) as exc:
                    status, body, retry_after = 0, b"", None
                    last_problem = f"network error ({type(exc).__name__})"
                self._last = time.monotonic()
            if status in (200, 404):
                return status, body
            if status:
                last_problem = f"HTTP {status}"
            delay = 2**attempt * 2.0
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 60.0))  # honour the server's hint, capped
            if attempt < self.retries - 1:
                await self._sleep(delay)
        raise ProviderUnavailable(f"{last_problem} after {self.retries} attempts")


# ---- pure parsers (no I/O) -------------------------------------------------


def decode_text(body: bytes) -> str:
    """Decode a response that may contain non-UTF-8 bytes (clan names use a raw nbsp byte)."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def parse_clan_roster(body: bytes) -> list[ClanMate]:
    """Parse the clan CSV. Raises ``ProviderUnavailable`` if it is not the expected CSV at all."""
    text = decode_text(body).replace("\xa0", " ")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip().lower() for c in rows[0][:3]] != ["clanmate", "clan rank", "total xp"]:
        raise ProviderUnavailable("Unexpected clan roster response (header missing)")
    mates: list[ClanMate] = []
    for row in rows[1:]:
        if len(row) < 3 or not row[0].strip():
            continue  # blank or truncated line
        try:
            kills = int(row[3]) if len(row) > 3 and row[3].strip() else 0
            mates.append(ClanMate(row[0].strip(), row[1].strip(), int(row[2]), kills))
        except ValueError:
            continue  # one bad row must not discard the whole roster
    return mates


def parse_hiscores(body: bytes) -> dict[str, int]:
    """Parse hiscores JSON into ``{skill: xp}`` including ``Overall``; unranked (-1) skills are omitted."""
    try:
        data = json.loads(decode_text(body))
        skills = {s["name"]: int(s["xp"]) for s in data["skills"] if int(s["xp"]) >= 0}
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderUnavailable("Unexpected hiscores response") from exc
    if "Overall" not in skills:
        raise ProviderUnavailable("Hiscores response has no Overall entry")
    return skills


def parse_profile(body: bytes) -> Profile:
    """Parse a RuneMetrics profile; private profiles yield ``private=True``, unknown names raise a miss."""
    try:
        data = json.loads(decode_text(body))
    except ValueError as exc:
        raise ProviderUnavailable("Unexpected RuneMetrics response (not JSON)") from exc
    if not isinstance(data, dict):
        raise ProviderUnavailable("Unexpected RuneMetrics response shape")
    error = data.get("error")
    if error == "PROFILE_PRIVATE":
        return Profile(private=True, activities=[])
    if error in ("NO_PROFILE", "NOT_A_MEMBER"):
        raise PlayerNotFound(f"RuneMetrics has no profile for that name ({error})")
    if error:
        raise ProviderUnavailable(f"RuneMetrics error: {error}")
    raw = data.get("activities")
    if not isinstance(raw, list):
        raise ProviderUnavailable("RuneMetrics response has no activities list")
    acts = [
        Activity(str(a["date"]), str(a["text"]), str(a.get("details", "")))
        for a in raw
        if isinstance(a, dict) and "date" in a and "text" in a
    ]
    return Profile(private=False, activities=acts)


def classify_activity(text: str) -> str | None:
    """Return 'cap' or 'visit' for a Clan Citadel log line (case-insensitive), else None."""
    t = text.lower()
    if "citadel" not in t:
        return None
    if "capped" in t:
        return "cap"
    if "visited" in t:
        return "visit"
    return None


class Rs3Provider(XpProvider):
    """Real provider built on ``HttpFetcher`` and the parsers above."""

    def __init__(self, fetcher: HttpFetcher | None = None):
        """Use the given fetcher (tests inject a fake) or a default polite one."""
        self.http = fetcher or HttpFetcher()

    async def fetch_player(self, rsn: str) -> dict[str, int]:
        """Hiscores for one player including Overall; unknown names raise ``PlayerNotFound``."""
        status, body = await self.http.get(HISCORE_URL, {"player": rsn})
        if status == 404:
            raise PlayerNotFound(f"No hiscores entry for {rsn}")
        return parse_hiscores(body)

    async def fetch_skills(self, rsn: str) -> dict[str, int]:
        """Per-skill XP without the Overall pseudo-skill (so '*' milestones cannot misfire on it)."""
        return {k: v for k, v in (await self.fetch_player(rsn)).items() if k != "Overall"}

    async def fetch_roster(self, clan_name: str) -> list[ClanMate]:
        """The public clan roster for ``clan_name``; an unknown clan raises ``PlayerNotFound``."""
        status, body = await self.http.get(CLAN_URL, {"clanName": clan_name})
        if status == 404:
            raise PlayerNotFound(f"No clan named {clan_name}")
        return parse_clan_roster(body)

    async def fetch_profile(self, rsn: str) -> Profile:
        """The latest 20 adventure-log entries (or a private-profile marker)."""
        status, body = await self.http.get(PROFILE_URL, {"user": rsn, "activities": "20"})
        if status == 404:
            raise PlayerNotFound(f"No RuneMetrics profile for {rsn}")
        return parse_profile(body)
