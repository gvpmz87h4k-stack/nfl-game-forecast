#!/usr/bin/env python3
"""Refresh the Sunday Desk snapshot using public NFL data feeds."""

import argparse
import csv
import io
import json
import os
import subprocess
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from scorecard import grade_predictions, record_predictions, summarize
from ratings import rating_view, ESPN_TO_NFLVERSE
from depth_chart import projected_starters, normalize as normalize_name
from situations import load_schedule, flags as situation_flags
from model import load_config, normal_cdf, predict_game, simulate_season, update_team_states

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SNAPSHOT = DATA_DIR / "snapshot.json"
LEDGER = DATA_DIR / "ledger.json"
PICKS = ROOT / "picks.json"
RATINGS = DATA_DIR / "ratings-current.json"
RATINGS_REPORT = DATA_DIR / "ratings-walkforward-2025.json"
CACHE = DATA_DIR / "location-cache.json"
OVERRIDES = ROOT / "overrides.json"
PREPARATION_HISTORY = DATA_DIR / "preparation-history.json"
TRAVEL_OVERRIDES = ROOT / "travel-overrides.json"

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
INJURIES = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
WEEKLY_ROSTERS = "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_{season}.csv"

TEAM_ALIASES = {"LA": "LAR", "WAS": "WSH"}

BASELINES = {
    "ARI": (4.0, "3-14"), "ATL": (7.0, "8-9"), "BAL": (11.0, "8-9"),
    "BUF": (11.0, "12-5"), "CAR": (7.0, "8-9"), "CHI": (9.0, "11-6"),
    "CIN": (10.0, "6-11"), "CLE": (6.0, "5-12"), "DAL": (9.5, "7-9-1"),
    "DEN": (10.0, "14-3"), "DET": (11.0, "9-8"), "GB": (10.0, "9-7-1"),
    "HOU": (10.0, "12-5"), "IND": (8.0, "8-9"), "JAX": (9.0, "13-4"),
    "KC": (10.0, "6-11"), "LV": (6.0, "3-14"), "LAC": (10.0, "11-6"),
    "LAR": (11.5, "12-5"), "MIA": (4.0, "7-10"), "MIN": (9.0, "9-8"),
    "NE": (10.0, "14-3"), "NO": (8.0, "6-11"), "NYG": (7.0, "4-13"),
    "NYJ": (5.5, "3-14"), "PHI": (10.0, "11-6"), "PIT": (8.0, "10-7"),
    "SF": (10.0, "12-5"), "SEA": (11.0, "14-3"), "TB": (8.0, "8-9"),
    "TEN": (6.0, "3-14"), "WSH": (7.0, "5-12"), "WAS": (7.0, "5-12")
}

WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 80: "Rain showers", 81: "Rain showers",
    82: "Heavy showers", 85: "Snow showers", 95: "Thunderstorms", 96: "Storms with hail"
}

COMMUNICATION_GROUPS = {
    "Protection unit": {"QB", "C", "G", "OG", "OT", "T", "TE", "RB"},
    "Coverage unit": {"CB", "DB", "S", "FS", "SS", "NB", "LB", "ILB", "OLB"},
    "Defensive front": {"DT", "NT", "DE", "DL", "LB", "ILB", "OLB"}
}

STATUS_WEIGHT = {
    "out": 1.0, "injured reserve": 1.0, "physically unable to perform": 1.0,
    "suspension": 1.0, "doubtful": 0.75, "questionable": 0.35,
    "day-to-day": 0.25, "probable": 0.12
}

POSITION_WEIGHT = {
    "QB": 1.8, "C": 1.65, "G": 1.35, "OG": 1.35, "OT": 1.35, "T": 1.35,
    "S": 1.3, "FS": 1.3, "SS": 1.3, "CB": 1.25, "DB": 1.25, "NB": 1.3,
    "LB": 1.15, "ILB": 1.15, "OLB": 1.15, "DT": 1.05, "NT": 1.05,
    "DE": 1.05, "DL": 1.05, "TE": 0.9, "RB": 0.8, "WR": 0.75
}

PREPARATION_WEIGHT = {
    "Missed practice": 1.0, "Limited practice": 0.5, "Personal designation": 0.55,
    "Official leave": 1.0, "Suspended": 1.0, "Game-day inactive": 1.0,
    "Commissioner exempt": 1.0, "Late activation": 0.45
}


def player_key(team, name):
    normalized = "".join(character.lower() for character in (name or "unknown") if character.isalnum())
    return f"{team}|{normalized}"


def normalize_preparation_history(history):
    normalized = {"players": {}, "rosterStatuses": {}, "schemaVersion": 2}
    for record in history.get("players", {}).values():
        team, name = record.get("team"), record.get("player")
        if not team or not name:
            continue
        key = player_key(team, name)
        target = normalized["players"].setdefault(key, {
            "team": team, "player": name, "position": record.get("position", ""), "observations": []
        })
        known = {(item.get("date"), tuple(sorted(item.get("categories", [])))) for item in target["observations"]}
        for observation in record.get("observations", []):
            categories = [category for category in observation.get("categories", []) if category != "Official leave"]
            categories = sorted(categories)
            signature = (observation.get("date"), tuple(sorted(categories)))
            if categories and signature not in known:
                target["observations"].append({"date": observation.get("date"), "categories": categories})
                known.add(signature)
    normalized["updatedAt"] = history.get("updatedAt")
    return normalized


def get_json(url, params=None, retries=2):
    if params:
        url = f"{url}?{urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                ["curl", "-fsSL", "--max-time", "20", url],
                check=True, capture_output=True, text=True
            )
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Could not load {url}: {exc}") from exc
            time.sleep(1 + attempt)


def get_text(url, retries=2):
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                ["curl", "-fsSL", "--max-time", "30", url],
                check=True, capture_output=True, text=True
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            if attempt == retries:
                raise RuntimeError(f"Could not load {url}: {exc}") from exc
            time.sleep(1 + attempt)


def load_json(path, default):
    try:
        with path.open() as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def team_strength(abbreviation):
    wins = BASELINES.get(abbreviation, (8.5, "0-0"))[0]
    return (wins - 8.5) * 1.35


def parse_margin(detail, home_abbr, away_abbr):
    """Return the expected HOME margin of victory: positive means the home team is favored.

    That is the convention the model uses everywhere (normal_cdf(margin / scale) is the
    home win probability, and the nflverse backtest feeds spread_line the same way).
    Market text is written from the favorite's side ("SEA -3.5" = Seattle favored by
    3.5), so a home favorite's negative number becomes a positive home margin, and a
    bare number is treated as an ESPN home-relative spread (negative = home favored).
    """
    if not detail:
        return None
    text = detail.replace("−", "-").replace("PK", "0")
    matcher = re.finditer(r"\b([A-Z]{2,4})\s*([+-]?\d+(?:\.\d+)?)\b", text)
    for item in matcher:
        team = item.group(1)
        if team not in {home_abbr, away_abbr}:
            continue
        value = float(item.group(2))
        return -value if team == home_abbr else value
    numeric = re.search(r"([-+]?\d+(?:\.\d+)?)", text)
    if numeric:
        return -float(numeric.group(1))
    return None


def parse_over_under(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def american_to_probability(odds):
    """+150 -> 0.4, -180 -> 0.643. Includes the book's fee; pair with devig()."""
    try:
        value = float(str(odds).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    return 100.0 / (value + 100.0) if value > 0 else -value / (-value + 100.0)


def devig(home_odds, away_odds):
    """The market's own home win probability with the book's fee stripped out."""
    h, a = american_to_probability(home_odds), american_to_probability(away_odds)
    if h is None or a is None or h + a <= 0:
        return None
    return round(h / (h + a), 4)


def parse_odds(competition, home_abbr, away_abbr):
    """Everything ESPN's odds object tells us, in the model's convention (positive = home favored)."""
    odds = (competition.get("odds") or [{}])[0]
    detail = odds.get("details") or ""
    total = odds.get("overUnder")
    spread_block = odds.get("pointSpread") or {}
    money_block = odds.get("moneyline") or {}

    def line_of(side, when):
        value = ((spread_block.get(side) or {}).get(when) or {}).get("line")
        try:
            return float(str(value).replace("+", "")) if value not in (None, "") else None
        except ValueError:
            return None

    def price_of(block, side, when):
        return ((block.get(side) or {}).get(when) or {}).get("odds")

    current = parse_margin(detail, home_abbr, away_abbr)
    home_line_close = line_of("home", "close")
    if home_line_close is not None:
        current = -home_line_close                  # ESPN writes the home line from the home side: -3.5 = favored by 3.5
    if current is None and isinstance(odds.get("spread"), (int, float)):
        current = -float(odds["spread"])
    home_line_open = line_of("home", "open")
    opening = -home_line_open if home_line_open is not None else current
    line_movement = round(current - opening, 4) if (current is not None and opening is not None) else 0.0
    moneyline = {
        "home": price_of(money_block, "home", "close"), "away": price_of(money_block, "away", "close"),
        "homeOpen": price_of(money_block, "home", "open"), "awayOpen": price_of(money_block, "away", "open"),
    }
    return {
        "detail": detail, "total": total, "homeMargin": current,
        "openingHomeMargin": opening, "lineMovement": line_movement,
        "book": (odds.get("provider") or {}).get("name"),
        "spreadPrice": {"home": price_of(spread_block, "home", "close"), "away": price_of(spread_block, "away", "close"),
                        "homeOpen": price_of(spread_block, "home", "open"), "awayOpen": price_of(spread_block, "away", "open")},
        "moneyline": moneyline,
        "moneylineHomeProbability": devig(moneyline["home"], moneyline["away"]),
        "moneylineOpenHomeProbability": devig(moneyline["homeOpen"], moneyline["awayOpen"]),
    }


def parse_record(record):
    if not record:
        return None
    match = re.match(r"(\d+)\-(\d+)", record)
    if not match:
        return None
    wins = int(match.group(1))
    losses = int(match.group(2))
    played = wins + losses
    if played == 0:
        return None
    return wins / played


def extract_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_signal_overrides(raw, key):
    payload = raw.get(key)
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        if payload.get("difference") is not None:
            candidate = extract_float(payload.get("difference"))
            if candidate is not None:
                return candidate
        home_value = payload.get("home")
        away_value = payload.get("away")
        if home_value is not None and away_value is not None:
            home_float = extract_float(home_value)
            away_float = extract_float(away_value)
            if home_float is not None and away_float is not None:
                candidate = home_float - away_float
                return candidate
        opening = payload.get("opening")
        current = payload.get("current")
        if opening is not None and current is not None:
            opening_value = extract_float(opening)
            current_value = extract_float(current)
            if opening_value is not None and current_value is not None:
                return current_value - opening_value
    return None


def compute_line_movement_signal(odds, override):
    difference = extract_signal_overrides(override, "lineMovement")
    if difference is None:
        difference = odds.get("lineMovement", 0.0)
        source = "market line movement" if difference else "no market line movement"
    else:
        source = "manual override"
    return {
        "difference": float(difference),
        "probabilityShift": 0.0,
        "source": source,
        "available": bool(difference),
        "applied": False
    }


def active_injuries(raw, team_id_to_abbr):
    result = {}
    for team in raw.get("injuries", []):
        abbr = team_id_to_abbr.get(str(team.get("id")))
        if not abbr:
            continue
        players = []
        seen = set()
        for item in team.get("injuries", []):
            status = item.get("status", "Unknown")
            if status.lower() in {"active", "healthy"}:
                continue
            athlete = item.get("athlete") or {}
            player_id = athlete.get("id") or athlete.get("displayName")
            if player_id in seen:
                continue
            seen.add(player_id)
            details = item.get("details") or {}
            position = (athlete.get("position") or {}).get("abbreviation", "")
            players.append({
                "player": athlete.get("displayName", "Unknown player"),
                "position": position,
                "status": status,
                "detail": details.get("type") or details.get("detail") or "",
                "note": item.get("shortComment", ""),
                "updated": item.get("date")
            })
        priority = {"Out": 0, "Doubtful": 1, "Questionable": 2, "Day-To-Day": 3}
        players.sort(key=lambda player: (priority.get(player["status"], 9), player["player"]))
        result[abbr] = players
    return result


def report_preparation_events(raw, team_id_to_abbr):
    events = []
    for team in raw.get("injuries", []):
        abbr = team_id_to_abbr.get(str(team.get("id")))
        if not abbr:
            continue
        for item in team.get("injuries", []):
            athlete = item.get("athlete") or {}
            details = item.get("details") or {}
            text = " ".join(str(value) for value in (
                item.get("status", ""), item.get("shortComment", ""),
                details.get("type", ""), details.get("detail", "")
            ) if value).lower()
            categories = []
            if any(phrase in text for phrase in ("did not practice", "didn't practice", "non-participant", " dnp")):
                categories.append("Missed practice")
            elif any(phrase in text for phrase in ("limited practice", "limited participant", "limited participation")):
                categories.append("Limited practice")
            if any(phrase in text for phrase in ("personal", "not injury related", "non-injury related")):
                categories.append("Personal designation")
            if item.get("status", "").lower() in {"leave", "personal leave", "bereavement"}:
                categories.append("Official leave")
            if "suspend" in text:
                categories.append("Suspended")
            if item.get("status", "").lower() == "inactive":
                categories.append("Game-day inactive")
            if not categories:
                continue
            events.append({
                "key": player_key(abbr, athlete.get("displayName")),
                "team": abbr, "player": athlete.get("displayName", "Unknown player"),
                "position": (athlete.get("position") or {}).get("abbreviation", ""),
                "categories": sorted(set(categories)), "updated": item.get("date")
            })
    return events


def roster_preparation_events(rows, season, week, prior_statuses, now):
    eligible = [row for row in rows if row.get("season") == str(season) and row.get("game_type") == "REG"]
    available_weeks = [int(row["week"]) for row in eligible if row.get("week") and int(row["week"]) <= week]
    if not available_weeks:
        return [], prior_statuses
    selected_week = max(available_weeks)
    current = [row for row in eligible if int(row.get("week") or 0) == selected_week]
    events = []
    next_statuses = dict(prior_statuses)
    roster_categories = {"SUS": "Suspended", "EXE": "Commissioner exempt", "INA": "Game-day inactive"}
    for row in current:
        team = TEAM_ALIASES.get(row.get("team"), row.get("team"))
        key = player_key(team, row.get("full_name"))
        status = row.get("status", "")
        categories = []
        if status in roster_categories:
            categories.append(roster_categories[status])
        if status == "ACT" and prior_statuses.get(key) and prior_statuses[key] != "ACT":
            categories.append("Late activation")
        if categories:
            events.append({
                "key": key, "team": team, "player": row.get("full_name", "Unknown player"),
                "position": row.get("depth_chart_position") or row.get("position", ""),
                "categories": categories, "updated": now.isoformat().replace("+00:00", "Z")
            })
        next_statuses[key] = status
    return events, next_statuses


def preparation_profiles(events, history, teams, now, config):
    player_history = history.setdefault("players", {})
    current = {}
    for event in events:
        existing = current.setdefault(event["key"], {**event, "categories": []})
        existing["categories"] = sorted(set(existing["categories"] + event["categories"]))
        if event.get("updated"):
            existing["updated"] = event["updated"]

    today = now.date().isoformat()
    for key, event in current.items():
        record = player_history.setdefault(key, {
            "team": event["team"], "player": event["player"],
            "position": event["position"], "observations": []
        })
        record.update({field: event[field] for field in ("team", "player", "position")})
        observation_date = (event.get("updated") or today)[:10]
        signature = (observation_date, tuple(sorted(event["categories"])))
        known = {(item["date"], tuple(sorted(item["categories"]))) for item in record["observations"]}
        if signature not in known:
            record["observations"].append({"date": observation_date, "categories": sorted(event["categories"])})
        record["observations"] = record["observations"][-20:]

    profiles = {team: {"score": 100, "level": "Stable", "rawRisk": 0.0, "items": [], "clusters": []} for team in teams}
    for key, record in player_history.items():
        observations = record.get("observations", [])
        if not observations or record.get("team") not in profiles:
            continue
        latest = max(observations, key=lambda item: item["date"])
        try:
            days = max(0, (now.date() - datetime.fromisoformat(latest["date"]).date()).days)
        except ValueError:
            days = 0
        if days > 7:
            continue
        categories = sorted(current.get(key, {}).get("categories", latest["categories"]))
        decay = 1.0 if key in current else config.get("preparationDailyDecay", 0.65) ** days
        missed = len({item["date"] for item in observations if "Missed practice" in item["categories"]})
        limited = len({item["date"] for item in observations if "Limited practice" in item["categories"]})
        severity = max(PREPARATION_WEIGHT.get(category, 0.0) for category in categories)
        risk = severity * POSITION_WEIGHT.get(record.get("position", ""), 0.65) * (1 + min(3, missed) * 0.15) * decay
        if risk <= 0:
            continue
        profiles[record["team"]]["items"].append({
            "player": record["player"], "position": record.get("position", ""),
            "categories": categories, "missedPracticeReports": missed,
            "limitedPracticeReports": limited, "daysSinceFlag": days,
            "decay": round(decay, 2), "risk": round(risk, 2)
        })

    for team, profile in profiles.items():
        base = sum(item["risk"] for item in profile["items"])
        interaction = 0.0
        for name, positions in COMMUNICATION_GROUPS.items():
            members = [item for item in profile["items"] if item["position"] in positions]
            if len(members) >= 2:
                interaction += (len(members) - 1) * sum(item["risk"] for item in members) * 0.35
                profile["clusters"].append(f"{name}: {len(members)} preparation flags")
        risk = base + interaction
        profile["rawRisk"] = round(risk, 2)
        profile["score"] = max(35, 100 - round(risk * 6))
        profile["level"] = "High" if profile["score"] < 65 else "Watch" if profile["score"] < 82 else "Stable"
        profile["items"].sort(key=lambda item: (-item["risk"], item["player"]))
        profile["items"] = profile["items"][:8]
    history["updatedAt"] = now.isoformat().replace("+00:00", "Z")
    return profiles


def travel_team_score(team):
    components = {
        "sleepCycles": min(6, max(0, float(team.get("localSleepCycles", 0)))),
        "localPractice": min(4, max(0, float(team.get("plannedLocalPracticeSessions", 0)))) * 0.5,
        "internationalTeam": min(5, max(0, float(team.get("internationalGames", 0)))) * 0.15,
        "internationalCoach": min(3, max(0, float(team.get("coachInternationalGames", 0)))) * 0.25,
        "surfaceFamiliarity": min(1, max(0, float(team.get("surfaceFamiliarity", 0)))),
        "lateTravel": -min(6, max(0, float(team.get("separateOrLateTravelers", 0)))) * 0.75
    }
    return round(sum(components.values()), 2), {key: round(value, 2) for key, value in components.items()}


def build_travel_profile(raw):
    if not raw or not isinstance(raw, dict):
        return {"available": False, "applied": False, "probabilityShift": 0.0, "uncertaintyMultiplier": 1.0}
    away_team = raw.get("away") or {}
    home_team = raw.get("home") or {}
    away_score, away_components = travel_team_score(away_team)
    home_score, home_components = travel_team_score(home_team)
    away_nights = float(away_team.get("localSleepCycles") or 0)
    home_nights = float(home_team.get("localSleepCycles") or 0)
    return {
        **raw, "available": True, "applied": False, "probabilityShift": 0.0,
        "nightsGap": home_nights - away_nights,           # home minus away nights slept on local time
        "restedSide": "home" if home_nights > away_nights else "away" if away_nights > home_nights else None,
        "uncertaintyProbabilityShift": 0.0,
        "shared": raw.get("shared", {}),
        "sources": raw.get("sources", []),
        "uncertaintyMultiplier": raw.get("uncertaintyMultiplier", 1.0),
        "away": {**away_team, "acclimationScore": away_score, "scoreComponents": away_components},
        "home": {**home_team, "acclimationScore": home_score, "scoreComponents": home_components}
    }


def find_location(city, state, country, cache):
    key = "|".join(filter(None, [city, state, country]))
    if key in cache:
        return cache[key]
    search = get_json(GEOCODE, {"name": city, "count": 8, "language": "en", "format": "json"})
    choices = search.get("results", [])
    country_code = "US" if country == "USA" else None
    match = next((x for x in choices if (not country_code or x.get("country_code") == country_code) and (not state or x.get("admin1") == state)), None)
    match = match or next((x for x in choices if not country_code or x.get("country_code") == country_code), None)
    if not match:
        return None
    cache[key] = {"latitude": match["latitude"], "longitude": match["longitude"]}
    return cache[key]


def weather_for(game, cache, now):
    venue = game["venue"]
    if venue["indoor"]:
        return None
    kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00"))
    delta_days = (kickoff.date() - now.date()).days
    if delta_days < 0 or delta_days > 15:
        return None
    location = find_location(venue["city"], venue.get("state"), venue.get("country"), cache)
    if not location:
        return None
    day = kickoff.date().isoformat()
    raw = get_json(FORECAST, {
        **location,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto",
        "start_date": day, "end_date": day
    })
    daily = raw.get("daily") or {}
    if not daily.get("time"):
        return None
    high = round(daily["temperature_2m_max"][0])
    low = round(daily["temperature_2m_min"][0])
    rain = daily["precipitation_probability_max"][0]
    wind = round(daily["wind_speed_10m_max"][0])
    code = daily["weather_code"][0]
    return {
        "summary": WEATHER_CODES.get(code, "Forecast available"),
        "temperature": f"{low}-{high} F",
        "precipitation": f"{rain}%",
        "wind": f"{wind} mph",
        "source": "Open-Meteo"
    }


ROLE_WEIGHT = {"starter": 1.0, "backup": 0.35, "unlisted": 0.25}
LONG_TERM_DAYS = 7          # injured reserve older than this: the replacement has settled in
LONG_TERM_WEIGHT = 0.1


def player_role(player, lineup):
    """starter: first at his slot on the depth chart; backup: on the chart lower; unlisted: not on it."""
    if not lineup:
        return "starter"          # no chart read: count everyone, the old behaviour
    name = player.get("player", "")
    if name in lineup["starters"]:
        return "starter"
    return "backup" if name in lineup["all"] else "unlisted"


def continuity_profile(team, game, side, lineup=None, now=None):
    now = now or datetime.now(timezone.utc)
    weighted = []
    for player in team.get("injuries", []):
        status = player.get("status", "").lower()
        severity = STATUS_WEIGHT.get(status, 0.2)
        long_term = False
        if status in ("injured reserve", "physically unable to perform") and player.get("updated"):
            try:
                stamp = datetime.fromisoformat(str(player["updated"]).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age = (now - stamp).days
            except (ValueError, TypeError):
                age = 0                      # unreadable date: treat the absence as fresh, never crash the run
            if age > LONG_TERM_DAYS:
                severity, long_term = LONG_TERM_WEIGHT, True
        role = player_role(player, lineup)
        position = player.get("position", "")
        weighted.append({**player, "severity": severity, "positionWeight": POSITION_WEIGHT.get(position, 0.65),
                         "role": role, "roleWeight": ROLE_WEIGHT[role], "longTerm": long_term})

    base_risk = sum(item["severity"] * item["positionWeight"] * item["roleWeight"] * 1.45 for item in weighted)
    interaction_risk = 0.0
    clusters = []
    members_by_cluster = {}
    for name, positions in COMMUNICATION_GROUPS.items():
        # only starters who may actually miss the game make a cluster; backups, unlisted names,
        # and long-settled absences still count toward the base risk, at their lighter weight
        members = [item for item in weighted if item["position"] in positions
                   and item["role"] == "starter" and not item["longTerm"] and item["severity"] >= 0.25]
        if len(members) < 2:
            continue
        severity_sum = sum(item["severity"] * item["positionWeight"] * item["roleWeight"] for item in members)
        cluster_risk = (len(members) - 1) * severity_sum * 0.72
        interaction_risk += cluster_risk
        clusters.append(f"{name}: {len(members)} linked flags")
        members_by_cluster[name] = [
            {"player": item.get("player"), "position": item.get("position"), "status": item.get("status"),
             "role": item["role"], "weight": round(item["severity"] * item["roleWeight"], 2)}
            for item in sorted(members, key=lambda item: -item["severity"] * item["roleWeight"])
        ]

    context_multiplier = 1.0
    if game["venue"]["neutral"]:
        context_multiplier += 0.08
    elif side == "away":
        context_multiplier += 0.12
    summary = ((game.get("weather") or {}).get("summary") or "").lower()
    if any(term in summary for term in ("rain", "snow", "storm", "fog")):
        context_multiplier += 0.08

    total_risk = (base_risk + interaction_risk) * context_multiplier
    score = max(35, 100 - round(total_risk * 0.7))
    level = "High" if score < 65 else "Watch" if score < 82 else "Stable"
    setaside = [
        {"player": item.get("player"), "position": item.get("position"), "status": item.get("status"), "role": item["role"],
         "why": "long-term absence, replacement settled" if item["longTerm"] else "not a starter on the depth chart"}
        for item in weighted if item["longTerm"] or item["role"] != "starter"
    ]
    return {
        "score": score, "level": level, "clusters": clusters, "clusterMembers": members_by_cluster,
        "discounted": setaside, "lineupRead": bool(lineup),
        "interactionRisk": round(interaction_risk * context_multiplier, 2),
        "totalRisk": round(total_risk, 2)
    }


def add_continuity(game, lineups=None):
    lineups = lineups or {}
    away = continuity_profile(game["away"], game, "away", lineups.get(game["away"]["abbreviation"]))
    home = continuity_profile(game["home"], game, "home", lineups.get(game["home"]["abbreviation"]))
    game["continuity"] = {
        "away": away, "home": home, "probabilityShift": 0.0, "applied": False
    }


def build_game(event, week):
    competition = event["competitions"][0]
    competitors = {item["homeAway"]: item for item in competition["competitors"]}
    home_raw, away_raw = competitors["home"], competitors["away"]

    def team(raw):
        info = raw["team"]
        record = next((r["summary"] for r in raw.get("records", []) if r.get("name") == "overall"), "0-0")
        return {
            "id": str(info["id"]), "name": info["displayName"], "abbreviation": info["abbreviation"],
            "logo": info.get("logo", ""), "record": record, "score": int(raw.get("score") or 0), "injuries": []
        }

    home, away = team(home_raw), team(away_raw)
    venue_raw = competition.get("venue") or {}
    address = venue_raw.get("address") or {}
    odds = parse_odds(competition, home["abbreviation"], away["abbreviation"])
    neutral = bool(competition.get("neutralSite"))
    model_margin = team_strength(home["abbreviation"]) - team_strength(away["abbreviation"]) + (0 if neutral else 1.5)
    margin = odds["homeMargin"] if odds["homeMargin"] is not None else model_margin
    status = (event.get("status") or {}).get("type") or {}
    return {
        "id": str(event["id"]), "week": week, "kickoff": event["date"],
        "status": status.get("state", "pre"), "completed": bool(status.get("completed")),
        "home": home, "away": away,
        "venue": {
            "name": venue_raw.get("fullName", "Venue pending"), "city": address.get("city", "Location pending"),
            "state": address.get("state", ""), "country": address.get("country", ""),
            "indoor": bool(venue_raw.get("indoor")), "neutral": neutral
        },
        "odds": {
            "detail": odds["detail"], "total": odds["total"],
            "openingHomeMargin": odds["openingHomeMargin"], "lineMovement": odds["lineMovement"],
            "book": odds["book"], "spreadPrice": odds["spreadPrice"], "moneyline": odds["moneyline"],
            "moneylineHomeProbability": odds["moneylineHomeProbability"],
            "moneylineOpenHomeProbability": odds["moneylineOpenHomeProbability"],
        },
        "lineSource": "Market line" if odds["homeMargin"] is not None else "Preseason team-strength fallback",
        "marketHomeMargin": round(margin, 2),
        "homeMargin": round(margin, 2),
        "homeWinProbability": round(normal_cdf(margin / 13.86), 4),
        "probabilitySource": "Market line" if odds["homeMargin"] is not None else "Team strength model",
        "weather": None
    }


def infer_current_week(games, now):
    upcoming = [g for g in games if datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00")) >= now]
    return min(upcoming, key=lambda game: game["kickoff"])["week"] if upcoming else max(g["week"] for g in games)


def season_table(games, team_meta, simulations):
    totals = {abbr: 0.0 for abbr in team_meta}
    market_totals = {abbr: 0.0 for abbr in team_meta}
    for game in games:
        home, away = game["home"]["abbreviation"], game["away"]["abbreviation"]
        if game["completed"]:
            if game["home"]["score"] > game["away"]["score"]:
                home_prob = 1.0
            elif game["home"]["score"] < game["away"]["score"]:
                home_prob = 0.0
            else:
                home_prob = 0.5
        else:
            home_prob = game["homeWinProbability"]
        totals[home] = totals.get(home, 0) + home_prob
        totals[away] = totals.get(away, 0) + (1 - home_prob)
        market_home = game.get("marketHomeWinProbability")
        if market_home is not None:
            market_totals[home] = market_totals.get(home, 0) + market_home
            market_totals[away] = market_totals.get(away, 0) + (1 - market_home)

    teams = []
    for abbr, meta in team_meta.items():
        baseline, previous = BASELINES.get(abbr, (8.5, "0-0"))
        projected = totals.get(abbr, baseline)
        distribution = simulations.get(abbr, {})
        low, high = distribution.get("p10", 0), distribution.get("p90", 17)
        teams.append({
            **meta, "baselineWins": baseline, "previousRecord": previous,
            "marketProjectedWins": round(market_totals.get(abbr, 0.0), 1),
            "projectedWins": round(projected, 1), "range": f"{low}-{high}",
            "simulationMedian": distribution.get("median"),
            "simulationMean": distribution.get("mean")
        })
    return teams


def pretty_short(name):
    """'K.Cousins' as nflverse prints it -> 'K. Cousins'."""
    return re.sub(r"^([A-Z])\.(?=\S)", r"\1. ", name or "")


def starter_sentences(game):
    """One sentence per team whose quarterback is not the man last season's plays were built on."""
    out = []
    for side in ("away", "home"):
        entry = (game.get("starters") or {}).get(side) or {}
        name, rating_name, last = entry.get("name"), entry.get("ratingName"), entry.get("lastSeason")
        if not name:
            continue
        where = "per the depth chart" if entry.get("source", "").startswith("ESPN") else "per your override"
        team = game[side]["name"]
        if rating_name is None:
            out.append(f"{team} starts {name}, {where}; he has no plays in our ratings yet, so the rating uses last season's passing.")
        elif last and normalize_name(rating_name) != normalize_name(last):
            out.append(f"{team} starts {name}, {where}, not {pretty_short(last)}, who took most of last season's snaps.")
        if entry.get("status") and entry["status"].lower() not in ("active", "probable"):
            out.append(f"{name} is listed {entry['status']} on the injury report.")
    return out


def restore_frozen_market(games, ledger):
    """After kickoff the book drops the line from the feed and the readout would fall back to
    team strength. The ledger froze the closing line and prices, so put those back on the card."""
    for game in games:
        entry = ledger.get("games", {}).get(str(game["id"])) or {}
        if not entry.get("frozen") or entry.get("late") or entry.get("closingHomeMargin") is None:
            continue
        kept = entry.get("odds") or {}
        game["marketHomeMargin"] = entry["closingHomeMargin"]
        game["homeMargin"] = entry["closingHomeMargin"]
        game["lineSource"] = "Market line"
        game["lineNote"] = "closing line, kept from kickoff"
        game["probabilitySource"] = "Closing market line, frozen at kickoff"
        for key in ("detail", "total", "openingHomeMargin", "spreadPrice", "moneyline", "book"):
            if game["odds"].get(key) in (None, "", {}) and kept.get(key) not in (None, ""):
                game["odds"][key] = kept[key]
        if entry.get("closingHomeWinProbability") is not None:
            game["odds"]["moneylineHomeProbability"] = entry["closingHomeWinProbability"]


UNTESTED_POINTS_PER_ZONE = 0.74   # the schedule fit's value; it failed the blind test and is shown only as the generous case


def travel_worked_out(game, config):
    """Plain sentences showing exactly what the app did with a long trip, and what the generous
    jet-lag arithmetic would do instead, so nobody has to take the nudge on faith."""
    travel = game.get("travel") or {}
    if not travel.get("available"):
        return None
    home, away = game["home"]["abbreviation"], game["away"]["abbreviation"]
    names = {home: game["home"]["name"], away: game["away"]["name"]}
    h, a = travel.get("home") or {}, travel.get("away") or {}
    hn, an = float(h.get("localSleepCycles") or 0), float(a.get("localSleepCycles") or 0)
    hs, as_ = float(h.get("acclimationScore") or 0), float(a.get("acclimationScore") or 0)
    weight = float(config.get("travelScoreProbabilityWeight", 0.002))
    cap = float(config.get("travelProbabilityCap", 0.02))
    raw = (hs - as_) * weight
    shift = max(-cap, min(cap, raw))
    margin = game.get("marketHomeMargin")
    lines = [
        f"Nights slept on local time before kickoff: {names[away]} {an:g}, {names[home]} {hn:g}. Acclimation scores {as_:g} and {hs:g}, a gap of {abs(hs - as_):g}.",
        f"What the app did with that: it moved the win chance {abs(shift) * 100:.1f} percentage points toward {names[home] if shift > 0 else names[away]}, the gap times {weight:g}, and it can never move more than {cap * 100:g} points for travel. On the betting line that is worth about {abs(shift) / 0.0287:.1f} of a point.",
    ]
    tz = (travel.get("shared") or {}).get("timeZoneDifferenceHours")
    generous = None
    if tz is not None and margin is not None:
        body = abs(((float(tz) + 12) % 24) - 12)        # 17 hours ahead reads to the body as 7 hours behind
        unadj_h, unadj_a = max(0.0, body - hn), max(0.0, body - an)
        lines.append(
            f"Jet lag, rule of thumb: the body catches up about one hour a day. This venue is {abs(float(tz)):g} hours off, which the body feels as {body:g} hours. "
            f"So at kickoff {names[away]}'s body clock was about {unadj_a:g} hour{'s' if unadj_a != 1 else ''} off and {names[home]}'s about {unadj_h:g}."
        )
        penalty = UNTESTED_POINTS_PER_ZONE * (unadj_h - unadj_a)
        adjusted = margin - penalty
        p_home = normal_cdf(adjusted * float(config.get("probabilityCalibration", 1.1)) / 13.86)
        fav = home if adjusted > 0 else away
        lagged = names[home] if unadj_h > unadj_a else names[away]
        lines.append(
            f"The most generous case: suppose every hour a body clock is still off costs a team {UNTESTED_POINTS_PER_ZONE} of a point on the scoreboard. "
            f"That is the biggest value our own travel test produced, and the same test showed it does not predict results, so the app does not use it; it is here only to show the ceiling. "
            f"It would take about {abs(penalty):.1f} points from {lagged}: the line would go from {names[home] if margin > 0 else names[away]} by {abs(margin):g} to {names[fav]} by {abs(adjusted):.1f}, "
            f"about {round(max(p_home, 1 - p_home) * 100)} to {round(min(p_home, 1 - p_home) * 100)}."
        )
        generous = {"unadjustedZonesHome": unadj_h, "unadjustedZonesAway": unadj_a, "lineShiftPoints": round(penalty, 2), "adjustedHomeMargin": round(adjusted, 2), "homeWinProbability": round(p_home, 4)}
    if game.get("completed") and game["home"].get("score") is not None and margin is not None:
        miss = (game["home"]["score"] - game["away"]["score"]) - margin
        lines.append(f"Final: {names[home]} {game['home']['score']}, {names[away]} {game['away']['score']}. The result landed {abs(miss):.1f} points past the closing line on {names[home] if miss > 0 else names[away]}'s side. Even the most generous travel math explains at most {abs(generous['lineShiftPoints']) if generous else 0:.1f} of those points; the rest is football.")
    return {"shiftPercentagePoints": round(shift * 100, 2), "generous": generous, "lines": lines}


def app_vs_market(game, prediction):
    """Where the app's number differs from the market's, and why, in percentage points of the
    home side's win chance. Positive means toward the home team."""
    home, away = game["home"]["abbreviation"], game["away"]["abbreviation"]
    market = game.get("marketHomeWinProbability")
    spread_only = prediction["marketOnlyProbability"]
    app = prediction["homeWinProbability"]
    parts = [
        ("travel", prediction.get("travelProbabilityShift", 0.0)),
        ("injury clusters", prediction.get("continuityProbabilityShift", 0.0)),
        ("practice and roster status", prediction.get("preparationProbabilityShift", 0.0)),
        ("line movement", prediction.get("lineMovementProbabilityShift", 0.0)),
        ("shared uncertainty", prediction.get("uncertaintyProbabilityShift", 0.0)),
    ]
    named = sum(v for _, v in parts)
    parts.append(("rest and team form", app - spread_only - named))
    nudges = [{"label": label, "points": round(v * 100, 1), "toward": home if v > 0 else away} for label, v in parts if abs(v) >= 0.0005]
    gap = None if market is None else round((app - market) * 100, 1)
    nudge_gap = round((app - spread_only) * 100, 1)          # the app's own lean: what the nudges did to the line's number
    price_gap = None if market is None else round((market - spread_only) * 100, 1)   # win price versus spread, not an opinion
    return {
        "marketPct": None if market is None else round(market * 100, 1),
        "spreadOnlyPct": round(spread_only * 100, 1),
        "appPct": round(app * 100, 1),
        "gapPoints": gap,                                   # app minus market, toward home when positive
        "nudgeGapPoints": nudge_gap,
        "priceGapPoints": price_gap,
        "leans": None if abs(nudge_gap) < 0.05 else (home if nudge_gap > 0 else away),
        "nudges": nudges,
    }


def readout(game):
    """Four to six plain sentences a person can read without knowing the vocabulary."""
    home, away = game["home"]["abbreviation"], game["away"]["abbreviation"]
    names = {home: game["home"]["name"], away: game["away"]["name"]}
    p = game["homeWinProbability"]
    fav, dog, pf = (home, away, p) if p >= 0.5 else (away, home, 1 - p)
    lines = []
    if pf < 0.55:
        lines.append(f"This one is close to a coin flip; {names[fav]} has the slight edge.")
    else:
        lines.append(f"{names[fav]} is expected to win, about {round(pf * 10)} times in 10.")
    versus = game.get("appVsMarket") or {}
    if versus.get("nudgeGapPoints") is not None and abs(versus["nudgeGapPoints"]) >= 1 and versus.get("leans"):
        nudges = sorted(versus.get("nudges") or [], key=lambda n: -abs(n["points"]))
        why = f", mostly because of {nudges[0]['label']}" if nudges else (", mostly because of travel" if (game.get("travel") or {}).get("workedOut") else "")
        lines.append(f"The app leans {abs(versus['nudgeGapPoints']):.1f} points toward {names[versus['leans']]} beyond what the line says{why}.")
    margin = game.get("marketHomeMargin")
    if game.get("lineSource") == "Market line" and margin is not None:
        market_fav = home if margin > 0 else away
        market_dog = away if margin > 0 else home
        head_start = abs(margin)
        opening = game["odds"].get("openingHomeMargin")
        move = game["odds"].get("lineMovement") or 0.0
        if head_start == 0:
            sentence = "The betting market has this as a pick-em, no head start either way."
        else:
            sentence = f"The betting market gives {names[market_dog]} a {head_start:g}-point head start"
            if opening is not None and abs(move) >= 0.5:
                toward = home if move > 0 else away
                sentence += f", moved from {abs(opening):g} since the line opened, toward {names[toward]}"
            else:
                sentence += ", unchanged since the line opened"
            sentence += "."
        prices = game["odds"].get("spreadPrice") or {}
        try:
            drift = float(str(prices.get("home")).replace("+", "")) - float(str(prices.get("homeOpen")).replace("+", ""))
        except (TypeError, ValueError):
            drift = 0.0
        if abs(move) < 0.5 and abs(drift) >= 8:
            sentence += f" The price has drifted toward {names[home] if drift > 0 else names[away]} without the number changing."
        lines.append(sentence)
    else:
        lines.append("No betting line yet; the number comes from team strength alone.")
    shifts = []
    for key, label in (("continuity", "injury clusters"), ("preparation", "practice and roster status"), ("travel", "travel")):
        block = game.get(key) or {}
        value = block.get("probabilityShift") or 0.0
        if block.get("applied") and abs(value) >= 0.005:
            shifts.append((label, value))
    rest = (game.get("modelAdjustments") or {}).get("restPoints") or 0.0
    if abs(rest) >= 0.3:
        shifts.append(("rest", rest / 13.86 * 0.4))
    if shifts:
        parts = [f"{label} {'tilt' if label == 'injury clusters' else 'tilts'} it toward {names[home] if value > 0 else names[away]}" for label, value in shifts]
        lines.append("Beyond the line, " + "; ".join(parts) + ", a little.")
    else:
        lines.append("Injuries, practice reports, rest, and travel do not change the picture.")
    lines.extend(starter_sentences(game))
    rm = game.get("ratingModel")
    if rm:
        gap = rm["disagreementPoints"]
        if abs(gap) < 1.0:
            lines.append("Our play-by-play ratings agree with the market.")
        else:
            lines.append(f"Our play-by-play ratings lean {names[rm['leans']]} by {abs(gap):.1f} points more than the market does"
                         + ("; last season those disagreements were wrong more often than right." if rm.get("flagged") else "."))
    experts = [(k, v) for k, v in (game.get("outsidePicks") or {}).items() if k.startswith("cbs-") and v.get("winner")]
    if experts:
        for_fav = sum(1 for _, v in experts if v["winner"] == fav)
        for_dog = len(experts) - for_fav
        if for_fav == for_dog:
            lines.append(f"The {len(experts)} CBS writers who have picked are split down the middle.")
        elif for_fav >= for_dog:
            lines.append(f"{for_fav} of the {len(experts)} CBS writers who have picked take {names[fav]}, the favorite.")
        else:
            lines.append(f"{for_dog} of the {len(experts)} CBS writers who have picked take {names[dog]}, the underdog.")
    situation = game.get("situation") or {}
    if situation.get("divisional"):
        lines.append("This is a divisional game, and those have run a little closer than the line in every season we tested.")
    for key, pick in (game.get("outsidePicks") or {}).items():
        # people's picks are named only once the game is over; the writers are public already
        if key == "cbs" or key.startswith("cbs-") or not (pick.get("winner") or pick.get("spread")) or not game.get("completed"):
            continue
        who = pick.get("label") or key
        if pick.get("winner"):
            lines.append(f"{who} took {names.get(pick['winner'], pick['winner'])}"
                         + (f", and {names.get(pick['spread'], pick['spread'])} against the spread." if pick.get("spread") else "."))
        else:
            lines.append(f"{who} took {names.get(pick['spread'], pick['spread'])} against the spread.")
    weather = game.get("weather") or {}
    summary = (weather.get("summary") or "").lower()
    if not game["venue"].get("indoor") and any(w in summary for w in ("rain", "snow", "storm", "wind")):
        lines.append(f"Weather could matter: {weather.get('summary')}, wind {weather.get('wind', 'unknown')}.")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--week", type=int, help="Week to prioritize for weather. Defaults to the next week.")
    parser.add_argument("--skip-weather", action="store_true", help="Refresh schedule and injuries without weather calls.")
    args = parser.parse_args()
    config = load_config()

    now = datetime.now(timezone.utc)
    games = []
    team_meta = {}
    team_id_to_abbr = {}
    for week in range(1, 19):
        raw = get_json(SCOREBOARD, {"dates": args.season, "seasontype": 2, "week": week, "limit": 100})
        for event in raw.get("events", []):
            game = build_game(event, week)
            games.append(game)
            for side in ("away", "home"):
                team = game[side]
                team_id_to_abbr[team["id"]] = team["abbreviation"]
                team_meta[team["abbreviation"]] = {
                    "id": team["id"], "name": team["name"], "abbreviation": team["abbreviation"], "logo": team["logo"]
                }

    current_week = args.week or infer_current_week(games, now)
    raw_injuries = get_json(INJURIES)
    injury_map = active_injuries(raw_injuries, team_id_to_abbr)
    for game in games:
        game["away"]["injuries"] = injury_map.get(game["away"]["abbreviation"], [])
        game["home"]["injuries"] = injury_map.get(game["home"]["abbreviation"], [])

    cache = load_json(CACHE, {})
    weather_errors = []
    if not args.skip_weather:
        for game in [item for item in games if item["week"] == current_week]:
            try:
                game["weather"] = weather_for(game, cache, now)
            except RuntimeError as exc:
                weather_errors.append(str(exc))

    preparation_history = load_json(PREPARATION_HISTORY, {"players": {}, "rosterStatuses": {}})
    if preparation_history.get("schemaVersion") != 2:
        preparation_history = normalize_preparation_history(preparation_history)
    preparation_warnings = []
    preparation_events = report_preparation_events(raw_injuries, team_id_to_abbr)
    try:
        roster_rows = list(csv.DictReader(io.StringIO(get_text(WEEKLY_ROSTERS.format(season=args.season)))))
        roster_events, roster_statuses = roster_preparation_events(
            roster_rows, args.season, current_week,
            preparation_history.get("rosterStatuses", {}), now
        )
        preparation_events.extend(roster_events)
        preparation_history["rosterStatuses"] = roster_statuses
    except RuntimeError as exc:
        preparation_warnings.append(str(exc))
    preparation_map = preparation_profiles(preparation_events, preparation_history, team_meta, now, config)

    ratings = load_json(RATINGS, None)
    manual_starters = load_json(OVERRIDES, {}).get("starters", {})
    qb_names = list((ratings or {}).get("qbName", {}).values())
    starters, starter_problems = projected_starters(team_id_to_abbr, get_json, qb_names, manual_starters)
    for problem in starter_problems:
        preparation_warnings.append(f"depth chart {problem}")
    lineups = {abbr: {"starters": set(entry.get("lineup") or []), "all": set(entry.get("lineup") or []) | set(entry.get("chartAll") or [])}
               for abbr, entry in starters.items() if entry.get("lineup")}

    for game in games:
        add_continuity(game, lineups)
        game["preparation"] = {
            "away": preparation_map.get(game["away"]["abbreviation"], {}),
            "home": preparation_map.get(game["home"]["abbreviation"], {}),
            "probabilityShift": 0.0, "applied": False,
            "coverage": "Official roster status and explicit public practice wording only"
        }
        game["lineMovement"] = {"difference": 0.0, "probabilityShift": 0.0, "source": "not loaded", "applied": False}

    ledger = load_json(LEDGER, {"season": args.season, "games": {}})
    if ledger.get("season") != args.season:
        ledger = {"season": args.season, "games": {}}
    restore_frozen_market(games, ledger)
    overrides = load_json(OVERRIDES, {"games": {}}).get("games", {})
    travel_overrides = load_json(TRAVEL_OVERRIDES, {"games": {}}).get("games", {})
    for game in games:
        game["travel"] = build_travel_profile(travel_overrides.get(game["id"]))
    states = {}
    last_kickoff = {}
    for game in sorted(games, key=lambda item: item["kickoff"]):
        home = game["home"]["abbreviation"]
        away = game["away"]["abbreviation"]
        kickoff = datetime.fromisoformat(game["kickoff"].replace("Z", "+00:00"))
        home_rest = min(14, max(0, (kickoff - last_kickoff[home]).days)) if home in last_kickoff else 7
        away_rest = min(14, max(0, (kickoff - last_kickoff[away]).days)) if away in last_kickoff else 7
        override = overrides.get(game["id"], {})
        home_points = float(override.get("home_points", 0))
        away_points = float(override.get("away_points", 0))
        apply_signal = game["week"] == current_week and not game["completed"]
        continuity_difference = (
            game["continuity"]["away"]["interactionRisk"] -
            game["continuity"]["home"]["interactionRisk"]
        )
        preparation_difference = (
            game["preparation"]["away"].get("rawRisk", 0) -
            game["preparation"]["home"].get("rawRisk", 0)
        )
        line_movement = compute_line_movement_signal(game["odds"], override)
        line_movement["applied"] = apply_signal
        game["lineMovement"] = line_movement
        travel_active = game["travel"].get("available", False) and not game["completed"] and game["week"] == current_week
        travel_score_difference = 0.0
        if travel_active:
            travel_score_difference = (
                game["travel"]["home"]["acclimationScore"] -
                game["travel"]["away"]["acclimationScore"]
            )
        prediction = predict_game(
            game["marketHomeMargin"], states.get(home, 0.0), states.get(away, 0.0),
            home_rest, away_rest, continuity_difference, apply_signal,
            home_points - away_points, config,
            preparation_difference=preparation_difference,
            apply_preparation=apply_signal,
            line_movement_difference=line_movement["difference"],
            apply_line_movement=apply_signal,
            travel_score_difference=travel_score_difference,
            uncertainty_multiplier=game["travel"].get("uncertaintyMultiplier", 1.0),
            apply_travel=travel_active
        )
        game["marketSpreadHomeWinProbability"] = prediction["marketOnlyProbability"]
        game["marketHomeWinProbability"] = game["odds"].get("moneylineHomeProbability") or prediction["marketOnlyProbability"]
        game["marketProbabilitySource"] = "moneyline, fee removed" if game["odds"].get("moneylineHomeProbability") else "spread converted"
        game["effectiveMargin"] = prediction["effectiveMargin"]
        game["homeMargin"] = prediction["effectiveMargin"]
        game["homeWinProbability"] = prediction["homeWinProbability"]
        game["appVsMarket"] = app_vs_market(game, prediction)
        frozen = ledger.get("games", {}).get(str(game["id"])) or {}
        if frozen.get("frozen") and not frozen.get("late") and frozen.get("homeWinProbability") is not None:
            # once a game has kicked off the card shows the forecast that was frozen, not a recompute
            # made after the nudges switched off; the ledger graded this exact number
            game["homeWinProbability"] = frozen["homeWinProbability"]
            game["forecastNote"] = "frozen at kickoff"
            if frozen.get("appVsMarket"):
                game["appVsMarket"] = frozen["appVsMarket"]
            else:
                # frozen before the breakdown existed: the numbers are the frozen ones, the itemised nudges are not kept
                close = frozen.get("closingHomeMargin")
                spread_only = normal_cdf(close * float(config.get("probabilityCalibration", 1.1)) / 13.86) if close is not None else None
                market = frozen.get("closingHomeWinProbability")
                gap = None if market is None else round((frozen["homeWinProbability"] - market) * 100, 1)
                nudge_gap = None if spread_only is None else round((frozen["homeWinProbability"] - spread_only) * 100, 1)
                game["appVsMarket"] = {
                    "marketPct": None if market is None else round(market * 100, 1),
                    "spreadOnlyPct": None if spread_only is None else round(spread_only * 100, 1),
                    "appPct": round(frozen["homeWinProbability"] * 100, 1), "gapPoints": gap,
                    "nudgeGapPoints": nudge_gap,
                    "priceGapPoints": None if market is None or spread_only is None else round((market - spread_only) * 100, 1),
                    "leans": None if nudge_gap is None or abs(nudge_gap) < 0.05 else (home if nudge_gap > 0 else away),
                    "nudges": [], "breakdownKept": False,
                }
        if game.get("travel", {}).get("available"):
            game["travel"]["workedOut"] = travel_worked_out(game, config)
        game["modelAdjustments"] = {
            "teamStatePoints": prediction["statePoints"],
            "restPoints": prediction["restPoints"],
            "spreadScale": prediction["spreadScale"],
            "homeRestDays": home_rest, "awayRestDays": away_rest
        }
        game["continuity"]["probabilityShift"] = prediction["continuityProbabilityShift"]
        game["continuity"]["applied"] = apply_signal
        game["preparation"]["probabilityShift"] = prediction["preparationProbabilityShift"]
        game["preparation"]["applied"] = apply_signal
        game["lineMovement"]["probabilityShift"] = prediction["lineMovementProbabilityShift"]
        game["travel"]["probabilityShift"] = prediction["travelProbabilityShift"]
        game["travel"]["uncertaintyProbabilityShift"] = prediction["uncertaintyProbabilityShift"]
        game["travel"]["uncertaintyMultiplier"] = prediction["uncertaintyMultiplier"]
        game["travel"]["applied"] = travel_active
        frozen_travel = (ledger.get("games", {}).get(str(game["id"])) or {})
        if frozen_travel.get("frozen") and not frozen_travel.get("late") and game["travel"].get("available"):
            # a finished game shows the travel nudge that was frozen with its forecast, not the switched-off one
            kept = frozen_travel.get("travelApplied")
            if kept and kept.get("probabilityShift") is not None:
                game["travel"].update({k: v for k, v in kept.items() if v is not None})
            else:
                worked = game["travel"].get("workedOut") or {}
                shift_pp = worked.get("shiftPercentagePoints")
                if shift_pp is not None:
                    game["travel"]["probabilityShift"] = round(shift_pp / 100, 4)
                game["travel"]["uncertaintyMultiplier"] = float((travel_overrides.get(game["id"]) or {}).get("uncertaintyMultiplier", game["travel"].get("uncertaintyMultiplier", 1.0)))
                game["travel"]["applied"] = True
            game["travel"]["frozenNote"] = "as frozen at kickoff"
        source_parts = [f"Calibrated {game['lineSource'].lower()}", "rolling team state", "rest"]
        if apply_signal:
            source_parts.append("continuity confidence")
            source_parts.append("preparation disruption")
            if line_movement["available"]:
                source_parts.append("line movement")
        if travel_active:
            source_parts.append("travel acclimation")
        if home_points or away_points:
            source_parts.append("saved override")
        game["probabilitySource"] = " + ".join(source_parts)

        if game["completed"]:
            actual_margin = game["home"]["score"] - game["away"]["score"]
            update_team_states(states, home, away, actual_margin, game["marketHomeMargin"], config)
        last_kickoff[home] = kickoff
        last_kickoff[away] = kickoff

    simulations = simulate_season(
        games, list(team_meta), simulations=5000,
        seed=args.season * 100 + current_week, config=config
    )

    ratings = load_json(RATINGS, None)
    ratings_report = load_json(RATINGS_REPORT, None)
    starter_overrides = {abbr: entry["ratingName"] for abbr, entry in starters.items() if entry.get("ratingName")}
    for game in games:
        game["starters"] = {}
        for side in ("home", "away"):
            abbr = game[side]["abbreviation"]
            entry = dict(starters.get(abbr) or {})
            if ratings:
                last_id = ratings.get("lastStarter", {}).get(ESPN_TO_NFLVERSE.get(abbr, abbr))
                entry["lastSeason"] = ratings.get("qbName", {}).get(last_id)
            game["starters"][side] = entry
        game["ratingModel"] = rating_view(
            ratings, ratings_report, game["home"]["abbreviation"], game["away"]["abbreviation"],
            game["marketHomeMargin"] if game["lineSource"] == "Market line" else None, starter_overrides
        )

    outside = load_json(PICKS, {"sources": {}}).get("sources", {})
    for game in games:
        game["outsidePicks"] = {}
        for source_key, source in outside.items():
            for pick in source.get("picks", []):
                if (pick.get("week") == game["week"] and pick.get("away") == game["away"]["abbreviation"]
                        and pick.get("home") == game["home"]["abbreviation"]):
                    game["outsidePicks"][source_key] = {
                        "label": source.get("label", source_key),
                        "winner": pick.get("winner"), "spread": pick.get("spread"),
                    }

    try:
        schedule = {(g["week"], g["home"], g["away"]): g for g in situation_flags(load_schedule({args.season}))}
    except Exception as exc:
        schedule = {}
        preparation_warnings.append(f"situations unavailable: {exc}")
    for game in games:
        key = (game["week"], ESPN_TO_NFLVERSE.get(game["home"]["abbreviation"], game["home"]["abbreviation"]),
               ESPN_TO_NFLVERSE.get(game["away"]["abbreviation"], game["away"]["abbreviation"]))
        sched = schedule.get(key)
        game["situation"] = {
            "divisional": bool(sched and sched["divisional"]),
            "home": (sched or {}).get("homeFlags", {}), "away": (sched or {}).get("awayFlags", {}),
            "note": "Divisional games have landed a little closer to the line than other games in every season tested; no other schedule factor held up.",
        } if sched else None
        game["readout"] = readout(game)

    now_iso = now.isoformat().replace("+00:00", "Z")
    for game in games:                     # frozen picks stay as frozen; new ones may still join
        entry = ledger.get("games", {}).get(str(game["id"])) or {}
        if entry.get("frozen") and not entry.get("late"):
            game["outsidePicks"] = {**(game.get("outsidePicks") or {}), **(entry.get("outsidePicks") or {})}
    record_predictions(ledger, games, now_iso)
    grade_predictions(ledger, games)
    scorecard = summarize(ledger)

    output = {
        "season": args.season, "currentWeek": current_week,
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "games": sorted(games, key=lambda game: game["kickoff"]),
        "teams": season_table(games, team_meta, simulations),
        "modelVersion": config["version"],
        "modelTrainingSeasons": config["trainedSeasons"],
        "modelConfig": {
            "probabilityCalibration": config["spreadScale"],
            "teamStateDecay": config["teamStateDecay"],
            "teamStateWeight": config["teamStateWeight"],
            "restDayWeight": config["restDayWeight"],
            "latentTeamSigma": config["latentTeamSigma"],
            "preparationProbabilityCap": config["preparationProbabilityCap"],
            "preparationDailyDecay": config["preparationDailyDecay"],
            "lineMovementProbabilityWeight": config["lineMovementProbabilityWeight"],
            "lineMovementProbabilityCap": config["lineMovementProbabilityCap"],
            "travelProbabilityCap": config["travelProbabilityCap"]
        },
        "scorecard": scorecard,
        "warnings": (weather_errors + preparation_warnings)[:5]
    }
    DATA_DIR.mkdir(exist_ok=True)
    temp_path = SNAPSHOT.with_suffix(".tmp")
    with temp_path.open("w") as handle:
        json.dump(output, handle, indent=2)
    os.replace(temp_path, SNAPSHOT)
    with CACHE.open("w") as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)
    preparation_temp = PREPARATION_HISTORY.with_suffix(".tmp")
    with preparation_temp.open("w") as handle:
        json.dump(preparation_history, handle, indent=2, sort_keys=True)
    os.replace(preparation_temp, PREPARATION_HISTORY)
    ledger_temp = LEDGER.with_suffix(".tmp")
    with ledger_temp.open("w") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
    os.replace(ledger_temp, LEDGER)
    overall = scorecard["overall"]
    if overall.get("graded"):
        print(f"Scorecard: {overall['graded']} graded, model {overall['modelAccuracy']:.1%} vs market {overall['marketAccuracy']:.1%}, "
              f"Brier {overall['modelBrier']:.4f} vs {overall['marketBrier']:.4f}.")
    else:
        print(f"Scorecard: nothing graded yet, {scorecard['open']} forecasts open, {scorecard['pending']} frozen.")
    print(f"Updated {len(games)} games, {len(output['teams'])} teams, Week {current_week} selected.")
    if weather_errors:
        print(f"Weather warnings: {len(weather_errors)}. The rest of the snapshot is usable.")


if __name__ == "__main__":
    main()
