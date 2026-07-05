"""
Throne & Liberty field boss / event notifier
=============================================

Two data sources, merged:

  schedule.yaml   Generic recurring daily pattern (times you always know,
                  e.g. "Dynamic Events at 03:00/06:00/...", or a boss that
                  reliably spawns at the same time every day).

  overrides.yaml  Date-specific known events (e.g. "on 2026-07-05, Americas
                  has a Guild Siege at 19:00"). When today has an override
                  for a given (region, time) slot, it REPLACES the generic
                  entry for that slot — so you get the specific name
                  instead of a vague placeholder — without you having to
                  touch schedule.yaml itself. Any slot without an override
                  just falls back to the generic entry as normal.

Region is configurable via the ACTIVE_REGION env var (or edit the
default below) so switching servers doesn't require touching code.

Setup
-----
    pip install -r requirements.txt
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    export ACTIVE_REGION="Americas"
    python bot.py
"""

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCHEDULE_FILE = Path(__file__).parent / "schedule.yaml"
OVERRIDES_FILE = Path(__file__).parent / "overrides.yaml"
STATE_FILE = Path(__file__).parent / "state.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PUT_YOUR_WEBHOOK_URL_HERE")

NOTIFY_MINUTES_BEFORE = int(os.environ.get("NOTIFY_MINUTES_BEFORE", "10"))

# Comma-separated if you ever want more than one, e.g. "Americas,Europe".
# Empty/unset = all regions in schedule.yaml.
ACTIVE_REGION = os.environ.get("ACTIVE_REGION", "Americas")
REGION_FILTER = [r.strip() for r in ACTIVE_REGION.split(",") if r.strip()]

# Empty list = all tiers (including Dynamic Events / guild events, tier: null).
TIER_FILTER: list[int] = []  # e.g. [3] for T3-only pings


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Spawn:
    region: str
    name: str
    tier: int | None
    at: datetime  # timezone-aware UTC
    source: str  # "override" or "generic" — just useful for debugging/logs

    @property
    def key(self) -> str:
        return f"{self.region}|{self.name}|{self.at.isoformat()}"

    @property
    def minutes_until(self) -> float:
        return (self.at - datetime.now(dt_timezone.utc)).total_seconds() / 60


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def slot_datetime(day: date, hh_mm: str, tz: ZoneInfo) -> datetime:
    hour, minute = map(int, hh_mm.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(dt_timezone.utc)


# ---------------------------------------------------------------------------
# Building the merged spawn list
# ---------------------------------------------------------------------------


def build_spawns(schedule: dict, overrides: dict, now: datetime) -> list[Spawn]:
    tz = ZoneInfo(schedule.get("timezone", "UTC"))
    today_local = now.astimezone(tz).date()
    # Look at today and tomorrow (in the schedule's timezone) so we catch
    # events just after local midnight too.
    days = [today_local, today_local + timedelta(days=1)]

    spawns: list[Spawn] = []
    # (date_str, region, time) -> True, for suppressing the matching
    # generic slot on that specific day.
    suppressed: set[tuple[str, str, str]] = set()

    # --- overrides first, so we know what to suppress in the generic pass ---
    for day in days:
        day_str = day.isoformat()
        day_overrides = overrides.get(day_str, {})
        for region, entries in day_overrides.items():
            if REGION_FILTER and region not in REGION_FILTER:
                continue
            for entry in entries:
                if TIER_FILTER and entry.get("tier") not in TIER_FILTER:
                    continue
                spawns.append(
                    Spawn(
                        region=region,
                        name=entry["name"],
                        tier=entry.get("tier"),
                        at=slot_datetime(day, entry["time"], tz),
                        source="override",
                    )
                )
                suppressed.add((day_str, region, entry["time"]))

    # --- generic recurring schedule, skipping suppressed slots ---
    for day in days:
        day_str = day.isoformat()
        for region, bosses in schedule.get("regions", {}).items():
            if REGION_FILTER and region not in REGION_FILTER:
                continue
            for boss in bosses:
                tier = boss.get("tier")
                if TIER_FILTER and tier not in TIER_FILTER:
                    continue
                for hh_mm in boss["times"]:
                    if (day_str, region, hh_mm) in suppressed:
                        continue
                    spawns.append(
                        Spawn(
                            region=region,
                            name=boss["name"],
                            tier=tier,
                            at=slot_datetime(day, hh_mm, tz),
                            source="generic",
                        )
                    )

    return spawns


# ---------------------------------------------------------------------------
# State (avoid double-notifying)
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified_keys": []}


def save_state(state: dict) -> None:
    state["notified_keys"] = state["notified_keys"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def format_message(spawn: Spawn) -> str:
    tier_label = f"T{spawn.tier} " if spawn.tier else ""
    mins = round(spawn.minutes_until)
    local_time = spawn.at.strftime("%H:%M UTC")
    tag = " 📌" if spawn.source == "override" else ""
    return f"⏰ **{tier_label}{spawn.name}**{tag} ({spawn.region}) in ~{mins} min — {local_time}"


def post_to_discord(messages: list[str]) -> None:
    if not messages:
        return
    if DISCORD_WEBHOOK_URL == "PUT_YOUR_WEBHOOK_URL_HERE":
        print("[warn] DISCORD_WEBHOOK_URL not set — printing instead:")
        for m in messages:
            print(m)
        return
    resp = requests.post(
        DISCORD_WEBHOOK_URL, json={"content": "\n".join(messages)}, timeout=10
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    schedule = load_yaml(SCHEDULE_FILE)
    overrides = load_yaml(OVERRIDES_FILE)
    now = datetime.now(dt_timezone.utc)

    spawns = build_spawns(schedule, overrides, now)

    state = load_state()
    notified = set(state["notified_keys"])

    to_send = []
    for spawn in spawns:
        if spawn.key in notified:
            continue
        if 0 <= spawn.minutes_until <= NOTIFY_MINUTES_BEFORE:
            to_send.append(spawn)
            notified.add(spawn.key)

    messages = [format_message(s) for s in sorted(to_send, key=lambda s: s.at)]
    post_to_discord(messages)

    state["notified_keys"] = list(notified)
    save_state(state)

    print(
        f"Region(s): {REGION_FILTER or 'all'} | "
        f"Checked {len(spawns)} scheduled spawns | "
        f"Sent {len(messages)} notifications."
    )


if __name__ == "__main__":
    main()
