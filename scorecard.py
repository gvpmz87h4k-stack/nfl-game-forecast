"""Prediction ledger and scorecard.

The ledger is the app's only honest evidence about the current season. Every updater run
records the latest forecast for each game that has not kicked off. The moment a game
kicks off, the last forecast recorded before it is frozen and can never change. When the
result arrives, the frozen forecast is graded against it, next to the market-only
probability for the same game, so the scorecard answers one question: does anything the
model adds beat the line it started from?

A game first seen after its kickoff is stored as "late" and is never graded, because a
forecast made with the result available is not a forecast.
"""

import math
from datetime import datetime


def _parse(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


ACCLIMATION_GAP_NIGHTS = 5     # a long-trip game counts when one side slept this many more nights on local time


def travel_fields(game):
    travel = game.get("travel") or {}
    if not travel.get("available") or travel.get("nightsGap") is None:
        return {}
    return {"travelNightsGap": travel["nightsGap"], "travelRestedSide": travel.get("restedSide")}


def grade_acclimation(entry):
    """For a long-trip game, did the better-rested side beat the closing line?"""
    gap = entry.get("travelNightsGap")
    if gap is None or abs(gap) < ACCLIMATION_GAP_NIGHTS or "acclimationCovered" in entry:
        return
    home_score, away_score, close = entry.get("homeScore"), entry.get("awayScore"), entry.get("closingHomeMargin")
    if home_score is None or away_score is None or close is None:
        return
    actual = home_score - away_score
    if actual == close:
        entry["acclimationPush"] = True
        return
    home_covered = actual > close
    entry["acclimationCovered"] = home_covered if gap > 0 else not home_covered


def adopt_outside_picks(entry, game):
    """Add any outside pick the frozen entry does not have yet. Never replaces one it has."""
    if entry.get("late"):
        return
    if entry.get("travelNightsGap") is None:
        entry.update(travel_fields(game))
    have = entry.setdefault("outsidePicks", {})
    for key, pick in (game.get("outsidePicks") or {}).items():
        if key not in have and (pick.get("winner") or pick.get("spread")):
            have[key] = dict(pick)


def grade_outside_picks(entry):
    """Grade every outside pick on a scored entry that is not graded yet."""
    home_score, away_score = entry.get("homeScore"), entry.get("awayScore")
    if home_score is None or away_score is None or home_score == away_score:
        return
    winner = entry["home"] if home_score > away_score else entry["away"]
    actual_margin = home_score - away_score
    close = entry.get("closingHomeMargin")
    for source_key, pick in (entry.get("outsidePicks") or {}).items():
        graded_pick = dict(pick)
        if pick.get("winner") in (entry["home"], entry["away"]) and "winnerCorrect" not in pick:
            graded_pick["winnerCorrect"] = pick["winner"] == winner
        if pick.get("spread") in (entry["home"], entry["away"]) and close is not None \
                and "spreadCovered" not in pick and "spreadPush" not in pick:
            if actual_margin == close:
                graded_pick["spreadPush"] = True        # landed exactly on the number: no win, no loss
            else:
                home_covered = actual_margin > close
                graded_pick["spreadCovered"] = home_covered if pick["spread"] == entry["home"] else not home_covered
        entry["outsidePicks"][source_key] = graded_pick


def record_predictions(ledger, games, now_iso):
    """Store or refresh the pre-kickoff forecast for every game; freeze at kickoff."""
    entries = ledger.setdefault("games", {})
    now = _parse(now_iso)
    for game in games:
        game_id = str(game["id"])
        kickoff = _parse(game["kickoff"])
        entry = entries.get(game_id)
        if entry and entry.get("frozen"):
            # People's picks reach the public file only once the game has kicked off (the
            # collector keeps them private before that, and accepts only picks entered before
            # kickoff), so they can land at or after the freeze. Take them in; they are graded
            # on the next pass if the game is already scored.
            adopt_outside_picks(entry, game)
            continue
        if now >= kickoff:
            if entry:
                entry["frozen"] = True
                entry["frozenAt"] = now_iso
                entry["closingHomeMargin"] = entry.get("marketHomeMargin")
                entry["closingHomeWinProbability"] = entry.get("marketHomeWinProbability")
                adopt_outside_picks(entry, game)
            else:
                entries[game_id] = {
                    "week": game["week"], "kickoff": game["kickoff"],
                    "away": game["away"]["abbreviation"], "home": game["home"]["abbreviation"],
                    "recordedAt": now_iso, "frozen": True, "late": True,
                }
            continue
        fresh = {
            "week": game["week"], "kickoff": game["kickoff"],
            "away": game["away"]["abbreviation"], "home": game["home"]["abbreviation"],
            "recordedAt": now_iso, "frozen": False, "late": False,
            "homeWinProbability": float(game["homeWinProbability"]),
            "marketHomeWinProbability": float(game.get("marketHomeWinProbability", game["homeWinProbability"])),
            "marketHomeMargin": game.get("marketHomeMargin"),
            "probabilitySource": game.get("probabilitySource", ""),
            "ratingHomeWinProbability": (game.get("ratingModel") or {}).get("ratingHomeWinProbability"),
            "ratingBlendHomeMargin": (game.get("ratingModel") or {}).get("blendHomeMargin"),
            "ratingFlagged": bool((game.get("ratingModel") or {}).get("flagged")),
            "outsidePicks": dict(game.get("outsidePicks") or {}),
            "odds": {k: (game.get("odds") or {}).get(k) for k in ("detail", "total", "openingHomeMargin", "spreadPrice", "moneyline", "book")},
            **travel_fields(game),
        }
        history = list(entry.get("history", [])) if entry else []
        point = {
            "at": now_iso, "marketHomeMargin": fresh["marketHomeMargin"],
            "homeWinProbability": fresh["homeWinProbability"],
            "marketHomeWinProbability": fresh["marketHomeWinProbability"],
        }
        if not history or any(history[-1][key] != point[key] for key in ("marketHomeMargin", "homeWinProbability")):
            history.append(point)
        fresh["history"] = history[-60:]
        first = entry.get("first") if entry else None
        fresh["first"] = first or {
            "at": now_iso, "marketHomeMargin": fresh["marketHomeMargin"],
            "homeWinProbability": fresh["homeWinProbability"],
            "marketHomeWinProbability": fresh["marketHomeWinProbability"],
        }
        entries[game_id] = fresh
    return ledger


def closing_line_value(entry):
    """Points the closing line moved toward the model's EARLY pick.

    The early pick is the side the model favored the first time it saw the game. If the
    line at kickoff moved toward that side, the value is positive: the model was ahead of
    the market. Requires a first line and a closing line; None otherwise.
    """
    first = entry.get("first") or {}
    close = entry.get("closingHomeMargin")
    early_margin = first.get("marketHomeMargin")
    early_p = first.get("homeWinProbability")
    if close is None or early_margin is None or early_p is None or early_p == 0.5:
        return None
    direction = 1.0 if early_p > 0.5 else -1.0
    return round((close - early_margin) * direction, 2)


def _log_loss(probability, outcome):
    p = min(0.999, max(0.001, probability))
    return -math.log(p if outcome else 1 - p)


def grade_predictions(ledger, games):
    """Grade frozen forecasts for completed games. Late entries are never graded."""
    entries = ledger.setdefault("games", {})
    for game in games:
        if not game.get("completed"):
            continue
        entry = entries.get(str(game["id"]))
        if not entry or entry.get("late") or not entry.get("frozen"):
            continue
        if entry.get("graded"):
            grade_outside_picks(entry)      # a pick that arrived after the first grading pass
            grade_acclimation(entry)
            continue
        home_score = game["home"].get("score")
        away_score = game["away"].get("score")
        if home_score is None or away_score is None:
            continue
        entry["homeScore"] = home_score
        entry["awayScore"] = away_score
        entry["graded"] = True
        if home_score == away_score:
            entry["tie"] = True
            continue
        outcome = 1 if home_score > away_score else 0
        p = entry["homeWinProbability"]
        m = entry["marketHomeWinProbability"]
        entry["tie"] = False
        entry["homeWon"] = bool(outcome)
        entry["modelPick"] = entry["home"] if p > 0.5 else entry["away"]
        entry["marketPick"] = entry["home"] if m > 0.5 else entry["away"]
        entry["modelCorrect"] = (p > 0.5) == (outcome == 1)
        entry["marketCorrect"] = (m > 0.5) == (outcome == 1)
        entry["modelBrier"] = round((p - outcome) ** 2, 4)
        entry["marketBrier"] = round((m - outcome) ** 2, 4)
        entry["modelLogLoss"] = round(_log_loss(p, outcome), 4)
        entry["marketLogLoss"] = round(_log_loss(m, outcome), 4)
        r = entry.get("ratingHomeWinProbability")
        if r is not None:
            entry["ratingCorrect"] = (r > 0.5) == (outcome == 1)
            entry["ratingBrier"] = round((r - outcome) ** 2, 4)
        grade_outside_picks(entry)
        grade_acclimation(entry)
        close = entry.get("closingHomeMargin")
        blend = entry.get("ratingBlendHomeMargin")
        if entry.get("ratingFlagged") and close is not None and blend is not None:
            actual_margin = home_score - away_score
            if actual_margin != close:
                entry["flaggedCovered"] = (blend > close) == (actual_margin > close)
    return ledger


def _block(entries):
    graded = [e for e in entries if e.get("graded") and not e.get("tie")]
    n = len(graded)
    if n == 0:
        return {"graded": 0}
    model_correct = sum(1 for e in graded if e["modelCorrect"])
    market_correct = sum(1 for e in graded if e["marketCorrect"])
    model_brier = sum(e["modelBrier"] for e in graded) / n
    market_brier = sum(e["marketBrier"] for e in graded) / n
    differ = [e for e in graded if e["modelPick"] != e["marketPick"]]
    rated = [e for e in graded if e.get("ratingBrier") is not None]
    flagged = [e for e in graded if e.get("flaggedCovered") is not None]
    sources = {}
    for e in graded:
        for key, pick in (e.get("outsidePicks") or {}).items():
            block = sources.setdefault(key, {"label": pick.get("label", key), "winnerPicks": 0, "winnerCorrect": 0, "spreadPicks": 0, "spreadCovered": 0, "spreadPushes": 0})
            if pick.get("spreadPush"):
                block["spreadPushes"] += 1
            if "winnerCorrect" in pick:
                block["winnerPicks"] += 1
                block["winnerCorrect"] += 1 if pick["winnerCorrect"] else 0
            if "spreadCovered" in pick:
                block["spreadPicks"] += 1
                block["spreadCovered"] += 1 if pick["spreadCovered"] else 0
    for block in sources.values():
        block["winnerAccuracy"] = round(block["winnerCorrect"] / block["winnerPicks"], 4) if block["winnerPicks"] else None
        block["spreadCoverRate"] = round(block["spreadCovered"] / block["spreadPicks"], 4) if block["spreadPicks"] else None
    return {
        "outsideSources": sources,
        "ratingGraded": len(rated),
        "ratingAccuracy": round(sum(1 for e in rated if e["ratingCorrect"]) / len(rated), 4) if rated else None,
        "ratingBrier": round(sum(e["ratingBrier"] for e in rated) / len(rated), 4) if rated else None,
        "flaggedPicks": len(flagged),
        "flaggedCovered": sum(1 for e in flagged if e["flaggedCovered"]),
        "graded": n,
        "ties": sum(1 for e in entries if e.get("graded") and e.get("tie")),
        "modelCorrect": model_correct, "marketCorrect": market_correct,
        "modelAccuracy": round(model_correct / n, 4), "marketAccuracy": round(market_correct / n, 4),
        "modelBrier": round(model_brier, 4), "marketBrier": round(market_brier, 4),
        "brierEdge": round(market_brier - model_brier, 4),
        "modelLogLoss": round(sum(e["modelLogLoss"] for e in graded) / n, 4),
        "marketLogLoss": round(sum(e["marketLogLoss"] for e in graded) / n, 4),
        "picksDiffered": len(differ),
        "modelWonDisagreements": sum(1 for e in differ if e["modelCorrect"]),
    }


def _clv_block(entries):
    frozen = [e for e in entries if e.get("frozen") and not e.get("late")]
    scored = []
    for entry in frozen:
        value = closing_line_value(entry)
        if value is None:
            continue
        first = entry["first"]
        early_market_home = (first.get("marketHomeWinProbability") or 0.5) > 0.5
        early_model_home = first["homeWinProbability"] > 0.5
        scored.append({
            "value": value,
            "disagreed": early_market_home != early_model_home,
            "moved": abs(entry["closingHomeMargin"] - first["marketHomeMargin"]) > 0.01,
        })
    if not scored:
        return {"games": 0}
    moved = [item for item in scored if item["moved"]]
    disagreed = [item for item in scored if item["disagreed"]]
    def mean(items):
        return round(sum(item["value"] for item in items) / len(items), 3) if items else None
    def positive_share(items):
        return round(sum(1 for item in items if item["value"] > 0) / len(items), 4) if items else None
    return {
        "games": len(scored),
        "linesThatMoved": len(moved),
        "meanPoints": mean(scored),
        "positiveShare": positive_share(scored),
        "movedMeanPoints": mean(moved),
        "movedPositiveShare": positive_share(moved),
        "disagreements": len(disagreed),
        "disagreementMeanPoints": mean(disagreed),
        "disagreementPositiveShare": positive_share(disagreed),
    }


def _acclimation_block(entries):
    """Long-trip games, one side slept five or more nights longer on local time: did that side cover?"""
    flagged = [e for e in entries if e.get("travelNightsGap") is not None and abs(e["travelNightsGap"]) >= ACCLIMATION_GAP_NIGHTS and e.get("frozen") and not e.get("late")]
    decided = [e for e in flagged if "acclimationCovered" in e]
    covered = sum(1 for e in decided if e["acclimationCovered"])
    return {
        "gapNights": ACCLIMATION_GAP_NIGHTS,
        "games": len(flagged), "decided": len(decided), "covered": covered,
        "pushes": sum(1 for e in flagged if e.get("acclimationPush")),
        "coverRate": round(covered / len(decided), 4) if decided else None,
        "list": [{"week": e["week"], "away": e["away"], "home": e["home"], "gap": e["travelNightsGap"],
                  "restedSide": e["home"] if e["travelNightsGap"] > 0 else e["away"],
                  "covered": e.get("acclimationCovered"), "push": e.get("acclimationPush", False)} for e in flagged],
    }


def summarize(ledger):
    """Season and per-week scorecard for the app."""
    entries = list(ledger.get("games", {}).values())
    weeks = sorted({e["week"] for e in entries})
    return {
        "season": ledger.get("season"),
        "overall": _block(entries),
        "closingLineValue": _clv_block(entries),
        "acclimation": _acclimation_block(entries),
        "pending": sum(1 for e in entries if e.get("frozen") and not e.get("graded") and not e.get("late")),
        "open": sum(1 for e in entries if not e.get("frozen")),
        "late": sum(1 for e in entries if e.get("late")),
        "weeks": [{"week": week, **_block([e for e in entries if e["week"] == week])} for week in weeks],
    }
