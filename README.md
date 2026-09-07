# Sunday Desk

Sunday Desk is a local NFL forecast dashboard. It refreshes the full regular-season schedule, current odds, public player status, and near-term outdoor weather. It then recalculates a coherent 32-team season win forecast from all game probabilities.

The experimental Communication Continuity Score looks for nonlinear risk when several unavailable or limited players sit inside the same protection, coverage, or defensive-front unit. Road, neutral-site, and difficult-weather contexts amplify the score. The selected week's interaction difference becomes a capped probability confidence shift. It cannot reverse the market's straight-up favorite by itself.

Model version 2 was selected with 2022-2024 regular-season games. The 2025 season stayed outside parameter selection and is the displayed holdout. The model calibrates spread probabilities, updates rolling team state only after a completed game, accounts for rest difference, and widens season outcomes through 5,000 simulations with persistent team-level uncertainty.

Model version 2.1 adds an experimental preparation-disruption layer for the selected week. It uses structured weekly roster states plus explicit public wording for missed or limited practice, personal designation, official leave, suspension, inactive status, and late activation. It stores observations locally so repeated missed-practice reports can accumulate and old flags decay after a return. It never infers private reasons or travel behavior.

Model version 2.2 adds a sourced travel-acclimation override for games with unusual verified logistics. The SF-LAR Melbourne entry records departure and arrival timing, estimated local sleep opportunities, local practice plans, verified late or separate travelers, international-game experience, and surface familiarity. Shared travel stress widens uncertainty. Only a measured difference between teams moves the win probability. These weights are experimental and are not part of the version 2.0 historical holdout.

Model version 2.3 added eight "differentiator" signals. Version 2.4 removed seven of them (pace and score script, clutch, scheme shock, coach aggression, official style, fan pressure, weather micro-risk): each was computed from a proxy such as the record or a keyword in the forecast rather than the thing its name described, three of them double-counted inputs already in the model, and none changed a pick in the 2025 replay. Line movement stays, computed from the first line the updater saw for a game against the current one, so it only carries information once the updater has run more than once for that game.

Version 2.4 also adds the scorecard. On every run the updater records the current forecast for each game that has not kicked off, freezes it at kickoff, and grades the frozen forecast after the result next to the market-only probability for the same game. The ledger lives in `data/ledger.json` and the summary in the snapshot. A game first seen after its kickoff is stored as late and never graded. This is the only evidence the app has about the current season; the 2025 replay used closing lines, which are sharper than the lines the app sees during the week.

## Rating model and closing line value (version 2.5)

`ratings.py` builds team ratings from nflverse play-by-play: opponent-adjusted expected points per play on offense and defense, weighted toward recent games, with the projected starting quarterback as his own component so a quarterback change moves the rating before any result does. A least-squares fit turns the ratings into a margin and a second fit blends that margin with the market line. Both fits use earlier seasons only.

Blind result, trained on 2022-2024 and tested on 2025 with closing lines: the market alone picked 65.3% of winners; the rating model alone 60.9%; the blend 65.7% with slightly worse probability error than the market. On the 78 games where the blend disagreed with the line by a point or more, it covered 38%. That is not an edge, and the app does not pretend it is: the rating model is displayed on every matchup and recorded in the ledger as a second opinion, never applied to the live probability. The 2026 scorecard grades it next to the model and the market, and grades its flagged disagreements against the closing line.

Closing line value is the other instrument. The ledger keeps every line the updater saw for a game, the first one and the close, and reports how far the close moved toward the model's first-seen pick. A model that is consistently ahead of the close has information; one that is not, does not.

```bash
python3 ratings.py --walkforward 2025     # blind replay, writes data/ratings-walkforward-2025.json
python3 ratings.py --current 2026         # live ratings, writes data/ratings-current.json
```

Play-by-play files download to `data/pbp/` on first use (about 19 MB per season). The 2026 file appears on nflverse once games have been played; until then the live ratings come from 2025 and earlier. Set a projected starter in `overrides.json` under `starters` when the last four games do not reflect who will play.

## How it runs without a laptop

The code lives in its own GitHub repository. A scheduled GitHub Actions job (`.github/workflows/update.yml`) runs the updater on GitHub's servers every morning at 10:00 Chicago and again at 17:00 on Thursday, Sunday, and Monday, ahead of the night kickoffs. Each run refreshes lines, injuries, weather, ratings, the ledger, and the scorecard, then commits the data files back to the repository. Netlify is connected to the repository and redeploys the site on every push, so the public page is always the latest run. Nothing depends on a laptop being open. The job can also be started by hand from the repository's Actions tab.

The ledger's freezing rule depends on that schedule: a forecast is only frozen if a run happened before kickoff. Two runs a day around game days is enough.

## Outside picks

`picks.json` holds picks from any outside source you want graded on the same terms as the model, for example the CBS Sports expert consensus. Enter a pick before kickoff with the week, both team codes as ESPN prints them, the straight-up winner, and optionally the side taken against the spread:

```json
{"week": 1, "away": "NE", "home": "SEA", "winner": "SEA", "spread": "NE"}
```

The next updater run attaches it to the game, the ledger freezes it at kickoff with everything else, and after the result it is graded straight up against the winner and, for spread picks, against the closing line. The scorecard reports each source's record. Picks entered after kickoff are ignored by the freeze rule. The file can be edited on GitHub from a phone; the scheduled run picks it up.

## Refresh the data

From this folder, run:

```bash
python3 update.py
```

To select a specific week for weather:

```bash
python3 update.py --week 1
```

Weather is only requested for outdoor games within the provider's forecast window. If weather fails, the schedule and player data are still saved.

## Open the dashboard

Run a local server:

```bash
python3 -m http.server 8765
```

Then open [http://localhost:8765](http://localhost:8765).

## Rebuild the blind historical replay

```bash
python3 backtest.py --season 2025
```

The replay stores each prediction before reading that game's result. Earlier completed games can update rolling team state for later games. The archived weekly injury report is available for 2025. The archived spread is a closing pregame line, not a fixed day-before snapshot.

## Late-news overrides

The matchup detail panel has a browser-only point adjustment. It is saved in local storage and does not alter the shared snapshot.

For a durable model adjustment, add a game ID to `overrides.json`:

```json
{
  "games": {
    "401872656": {
      "home_points": -2.5,
      "away_points": 0,
      "note": "Starting quarterback ruled out after the current line snapshot"
    }
  }
}
```

Run `python3 update.py` again after changing an override.

## Model boundaries

- Current market spread is the baseline when present, then the probability curve is calibrated with 2022-2024 games.
- Team win-total strength and venue are the fallback for games without a line.
- Rolling team state uses only surprises from earlier completed games, with decay so stale results matter less.
- Rest differences are capped before they become a point adjustment.
- Individual injury items are displayed. Clustered communication-unit injuries can shift probability by at most 2.5 percentage points, only for the selected week, and cannot flip the favorite by themselves.
- Preparation disruption can shift probability by at most 1.5 percentage points. It uses official roster states and explicit public practice wording only. Unsupported travel or personal circumstances are not guessed.
- Travel acclimation can shift probability by at most 2 percentage points. Unusual shared travel can also increase uncertainty and pull confidence toward 50 percent.
- Season ranges are the 10th and 90th percentiles from 5,000 simulations, not a fixed band around expected wins.
- Exact player health is private. The app only reports public status.
- Weather becomes useful close to kickoff. The app does not invent long-range forecasts.
- ESPN's feeds are public but undocumented, so the updater validates missing fields and fails with a clear message if the format changes.
