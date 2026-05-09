import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import aiohttp
import discord
from discord.ext import commands, tasks

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
DEBUG_STATS = os.getenv("DEBUG_STATS", "false").lower() == "true"

TEAM_FILTER = {
    team.strip().upper()
    for team in os.getenv("TEAM_FILTER", "").split(",")
    if team.strip()
}

TEAM_COLORS = {
    "ATL": 0xE03A3E,
    "CHI": 0x418FDE,
    "CONN": 0xDC4405,
    "DAL": 0x0C2340,
    "GS": 0x1D428A,
    "IND": 0x002D62,
    "LA": 0x702F8A,
    "LV": 0x000000,
    "MIN": 0x0C2340,
    "NY": 0x86CEBC,
    "PHX": 0xE56020,
    "SEA": 0x2C5234,
    "WAS": 0xE31837,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wnba_alert_bot")


@dataclass
class PlayerHit:
    game_id: str
    player_id: str
    player_name: str
    team_abbr: str
    opponent_abbr: str
    assists: int
    rebounds: int
    points: int
    threes_made: int
    minutes: str
    game_status: str
    game_period: int
    game_clock: str
    matchup: str
    alert_type: str
    player_photo_url: Optional[str] = None
    team_logo_url: Optional[str] = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.game_id}:{self.player_id}:{self.alert_type}"


def normalize_team_abbr(abbr: str) -> str:
    abbr = (abbr or "").upper().strip()
    aliases = {
        "CON": "CONN",
        "CONN": "CONN",
        "LVA": "LV",
        "LAS": "LA",
        "LAL": "LA",
        "NYL": "NY",
        "PHO": "PHX",
        "PHX": "PHX",
        "GSV": "GS",
        "GSW": "GS",
    }
    return aliases.get(abbr, abbr)


def get_team_color(team_abbr: str) -> int:
    return TEAM_COLORS.get(team_abbr.upper(), 0x2F3136)


def build_alert_embed(hit: PlayerHit) -> discord.Embed:
    if hit.alert_type == "early-watch":
        title = "📈 WNBA Q1 Stat Watch"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"

        if hit.assists >= 3 and hit.rebounds >= 3:
            tag = "🔥 All-Around"
        elif hit.assists >= 3:
            tag = "🎯 Playmaker"
        elif hit.rebounds >= 3:
            tag = "🧱 Glass Cleaner"
        else:
            tag = ""

        if tag:
            stat_line += f"\n{tag}"

        subtitle = "Early activity"

    elif hit.alert_type == "q2-stat-watch":
        title = "⚡ WNBA Q2 Stat Watch"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"

        if hit.assists >= 3 and hit.rebounds >= 3:
            tag = "🔥 All-Around"
        elif hit.assists >= 3:
            tag = "🎯 Playmaker"
        elif hit.rebounds >= 3:
            tag = "🧱 Glass Cleaner"
        else:
            tag = ""

        if tag:
            stat_line += f"\n{tag}"

        subtitle = "Mid-game activity"

    elif hit.alert_type == "triple-double-watch":
        title = "👀 WNBA Triple-Double Watch"
        stat_line = f"**{hit.points} PTS • {hit.rebounds} REB • {hit.assists} AST**"
        subtitle = "Q2 watch"

    elif hit.alert_type == "shes-on-fire":
        title = "🔥 She's On Fire"
        stat_line = f"**{hit.points} PTS • {hit.threes_made} 3PM**"
        subtitle = "Hot shooting start"

    else:
        title = "🚨 WNBA Double-Double Watch 🚨"
        stat_line = f"**{hit.assists} AST • {hit.rebounds} REB • {hit.points} PTS**"
        subtitle = "Q2 watch"

    embed = discord.Embed(
        title=title,
        description=f"**{hit.player_name}** ({hit.team_abbr})\n{stat_line}\n\n**{hit.matchup}**",
        color=get_team_color(hit.team_abbr),
    )

    if hit.team_logo_url:
        embed.set_footer(text=hit.team_abbr, icon_url=hit.team_logo_url)
    else:
        embed.set_footer(text=hit.team_abbr)

    embed.add_field(name="Game", value=f"{hit.game_status or 'Live'} • {subtitle}", inline=True)
    embed.add_field(name="Minutes", value=hit.minutes or "-", inline=True)

    if hit.game_clock:
        embed.add_field(name="Clock", value=hit.game_clock, inline=True)

    return embed


class WNBAAlertBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.alerted: Set[str] = set()
        self.rate_limited_until: float = 0.0

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.poll_live_games.start()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_live_games(self) -> None:
        try:
            log.info("Polling cycle started")

            if not DISCORD_CHANNEL_ID:
                log.warning("DISCORD_CHANNEL_ID is not set. Skipping poll.")
                return

            hits = await self.find_alert_hits()
            log.info("Found %s qualifying WNBA hits this cycle", len(hits))

            if not hits:
                return

            try:
                channel = await self.fetch_channel(DISCORD_CHANNEL_ID)
            except Exception:
                log.exception("Channel %s could not be fetched.", DISCORD_CHANNEL_ID)
                return

            for hit in hits:
                if hit.dedupe_key in self.alerted:
                    log.info("Skipping duplicate hit %s", hit.dedupe_key)
                    continue

                self.alerted.add(hit.dedupe_key)
                await self.send_player_alert(channel, hit)

        except Exception:
            log.exception("poll_live_games crashed this cycle")

    async def send_player_alert(self, channel, hit: PlayerHit) -> None:
        embed = build_alert_embed(hit)

        try:
            photo_bytes = None
            if hit.player_photo_url:
                photo_bytes = await self.fetch_image_bytes(hit.player_photo_url)

            if photo_bytes:
                file = discord.File(fp=io.BytesIO(photo_bytes), filename=f"{hit.player_id}.png")
                embed.set_thumbnail(url=f"attachment://{hit.player_id}.png")
                await channel.send(embed=embed, file=file)
            else:
                if hit.team_logo_url:
                    embed.set_thumbnail(url=hit.team_logo_url)
                await channel.send(embed=embed)

            log.info("Sent alert for %s", hit.dedupe_key)
        except Exception:
            log.exception("Failed to send alert for %s", hit.dedupe_key)

    async def safe_get_json(self, url: str, *, params: Optional[dict] = None, label: str = "request") -> Optional[dict]:
        assert self.session is not None

        now = time.time()
        if now < self.rate_limited_until:
            wait_left = int(self.rate_limited_until - now)
            log.warning("Skipping %s due to rate limit backoff (%ss left)", label, wait_left)
            return None

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params) as resp:
                    text = await resp.text()

                    if resp.status == 429:
                        retry_after_raw = resp.headers.get("Retry-After", "")
                        try:
                            retry_after = int(float(retry_after_raw))
                        except Exception:
                            retry_after = 30 * (attempt + 1)

                        retry_after = max(10, min(retry_after, 120))
                        self.rate_limited_until = time.time() + retry_after
                        log.warning("429 rate limited on %s. Backing off for %ss", label, retry_after)
                        return None

                    if resp.status != 200:
                        log.warning("%s failed with HTTP %s: %s", label, resp.status, text[:300])
                        return None

                    return await resp.json()

            except Exception:
                log.exception("%s failed on attempt %s", label, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))

        return None

    async def safe_get_bytes(self, url: str, *, label: str = "image") -> Optional[bytes]:
        assert self.session is not None

        now = time.time()
        if now < self.rate_limited_until:
            return None

        for attempt in range(2):
            try:
                async with self.session.get(url) as resp:
                    if resp.status == 429:
                        retry_after_raw = resp.headers.get("Retry-After", "")
                        try:
                            retry_after = int(float(retry_after_raw))
                        except Exception:
                            retry_after = 30

                        retry_after = max(10, min(retry_after, 120))
                        self.rate_limited_until = time.time() + retry_after
                        log.warning("429 rate limited on %s. Backing off for %ss", label, retry_after)
                        return None

                    if resp.status != 200:
                        log.info("%s fetch failed %s status=%s", label, url, resp.status)
                        return None

                    return await resp.read()

            except Exception:
                log.exception("%s fetch failed on attempt %s", label, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))

        return None

    async def fetch_image_bytes(self, url: str) -> Optional[bytes]:
        return await self.safe_get_bytes(url, label="player/team image")

    @poll_live_games.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    async def fetch_scoreboard(self) -> List[dict]:
        data = await self.safe_get_json(ESPN_SCOREBOARD_URL, label="WNBA scoreboard")
        if not data:
            return []

        events = data.get("events", []) or []
        log.info("Fetched %s WNBA games from ESPN scoreboard", len(events))
        return events

    async def fetch_summary(self, event_id: str) -> dict:
        data = await self.safe_get_json(
            ESPN_SUMMARY_URL,
            params={"event": event_id},
            label=f"WNBA summary {event_id}",
        )
        return data or {}

    def extract_competitors(self, event: dict) -> Dict[str, dict]:
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors", []) or []
        result = {}

        for comp in competitors:
            home_away = comp.get("homeAway")
            team = comp.get("team", {}) or {}
            abbr = normalize_team_abbr(team.get("abbreviation", ""))
            result[home_away] = {
                "abbr": abbr,
                "name": team.get("displayName", ""),
                "logo": team.get("logo"),
            }

        return result

    def get_matchup(self, competitors: Dict[str, dict]) -> str:
        away = competitors.get("away", {}).get("abbr", "AWAY")
        home = competitors.get("home", {}).get("abbr", "HOME")
        return f"{away} @ {home}"

    def parse_athlete_stats(self, stat_group: dict) -> List[dict]:
        labels = stat_group.get("labels") or stat_group.get("keys") or []
        athletes = stat_group.get("athletes", []) or []
        parsed = []

        for item in athletes:
            athlete = item.get("athlete", {}) or {}
            stats = item.get("stats", []) or []
            stat_map = {}

            for idx, label in enumerate(labels):
                if idx < len(stats):
                    stat_map[str(label).upper()] = stats[idx]

            parsed.append({"athlete": athlete, "stats": stat_map})

        return parsed

    def safe_int_stat(self, stat_map: dict, keys: List[str]) -> int:
        for key in keys:
            val = stat_map.get(key)
            if val is None:
                continue

            try:
                if isinstance(val, str) and "-" in val:
                    continue
                return int(float(val))
            except Exception:
                continue

        return 0

    def get_minutes(self, stat_map: dict) -> str:
        for key in ("MIN", "MINUTES"):
            if key in stat_map:
                return str(stat_map[key])
        return "0"

    async def find_alert_hits(self) -> List[PlayerHit]:
        try:
            games = await self.fetch_scoreboard()
        except Exception:
            log.exception("Could not fetch WNBA scoreboard")
            return []

        hits: List[PlayerHit] = []

        for event in games:
            game_id = str(event.get("id", ""))
            if not game_id:
                continue

            status = event.get("status", {}) or {}
            status_type = status.get("type", {}) or {}
            game_status = status_type.get("shortDetail") or status_type.get("detail") or status_type.get("description") or ""
            period = int(status.get("period", 0) or 0)
            clock = status.get("displayClock", "") or ""

            if period not in {1, 2}:
                continue

            competitors = self.extract_competitors(event)
            home_abbr = competitors.get("home", {}).get("abbr", "HOME")
            away_abbr = competitors.get("away", {}).get("abbr", "AWAY")

            if TEAM_FILTER and home_abbr not in TEAM_FILTER and away_abbr not in TEAM_FILTER:
                continue

            matchup = self.get_matchup(competitors)

            try:
                summary = await self.fetch_summary(game_id)
            except Exception:
                log.exception("Could not fetch WNBA summary for game %s", game_id)
                continue

            boxscore = summary.get("boxscore", {}) or {}
            players_groups = boxscore.get("players", []) or []

            for team_group in players_groups:
                team = team_group.get("team", {}) or {}
                team_abbr = normalize_team_abbr(team.get("abbreviation", ""))
                team_logo_url = team.get("logo")
                opponent_abbr = home_abbr if team_abbr == away_abbr else away_abbr

                stats_groups = team_group.get("statistics", []) or []

                for stat_group in stats_groups:
                    for parsed in self.parse_athlete_stats(stat_group):
                        athlete = parsed["athlete"]
                        stat_map = parsed["stats"]

                        player_id = str(athlete.get("id") or athlete.get("uid") or "unknown")
                        player_name = athlete.get("displayName") or athlete.get("shortName") or "Unknown Player"

                        player_photo_url = None
                        headshot = athlete.get("headshot")
                        if isinstance(headshot, dict):
                            player_photo_url = headshot.get("href")
                        elif isinstance(headshot, str):
                            player_photo_url = headshot

                        pts = self.safe_int_stat(stat_map, ["PTS", "POINTS"])
                        reb = self.safe_int_stat(stat_map, ["REB", "REBOUNDS"])
                        ast = self.safe_int_stat(stat_map, ["AST", "ASSISTS"])

                        three_pt = stat_map.get("3PT") or stat_map.get("3PM-A") or ""
                        threes_made = 0
                        if isinstance(three_pt, str) and "-" in three_pt:
                            try:
                                threes_made = int(three_pt.split("-", 1)[0])
                            except Exception:
                                threes_made = 0
                        else:
                            threes_made = self.safe_int_stat(stat_map, ["3PM", "3PTM"])

                        minutes = self.get_minutes(stat_map)

                        if DEBUG_STATS:
                            log.info(
                                "%s | %s | Q%s | PTS=%s REB=%s AST=%s 3PM=%s",
                                matchup, player_name, period, pts, reb, ast, threes_made
                            )

                        q1_stat_watch = period == 1 and (ast >= 3 or reb >= 3)
                        q2_stat_watch = period == 2 and (ast >= 3 or reb >= 3)
                        double_double_watch = period == 2 and ast >= 4 and reb >= 4
                        triple_double_watch = period == 2 and pts >= 5 and reb >= 4 and ast >= 4
                        shes_on_fire = (
                            (period == 1 and threes_made >= 2 and pts >= 8) or
                            (period == 2 and threes_made >= 3 and pts >= 12)
                        )

                        if not q1_stat_watch and not q2_stat_watch and not double_double_watch and not triple_double_watch and not shes_on_fire:
                            continue

                        common_data = dict(
                            game_id=game_id,
                            player_id=player_id,
                            player_name=player_name,
                            team_abbr=team_abbr,
                            opponent_abbr=opponent_abbr,
                            assists=ast,
                            rebounds=reb,
                            points=pts,
                            threes_made=threes_made,
                            minutes=minutes,
                            game_status=game_status,
                            game_period=period,
                            game_clock=clock,
                            matchup=matchup,
                            player_photo_url=player_photo_url,
                            team_logo_url=team_logo_url,
                        )

                        if q1_stat_watch:
                            hits.append(PlayerHit(**common_data, alert_type="early-watch"))
                        if q2_stat_watch:
                            hits.append(PlayerHit(**common_data, alert_type="q2-stat-watch"))
                        if double_double_watch:
                            hits.append(PlayerHit(**common_data, alert_type="double-double-watch"))
                        if triple_double_watch:
                            hits.append(PlayerHit(**common_data, alert_type="triple-double-watch"))
                        if shes_on_fire:
                            hits.append(PlayerHit(**common_data, alert_type="shes-on-fire"))

        return hits


bot = WNBAAlertBot()


@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command()
async def health(ctx: commands.Context) -> None:
    await ctx.send(
        f"Watching live WNBA games every {POLL_SECONDS}s. "
        f"Team filter: {', '.join(sorted(TEAM_FILTER)) or 'none'}. "
        f"Debug stats: {DEBUG_STATS}."
    )


if __name__ == "__main__":
    missing = []

    if not DISCORD_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")

    if not DISCORD_CHANNEL_ID:
        missing.append("DISCORD_CHANNEL_ID")

    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    bot.run(DISCORD_TOKEN)
