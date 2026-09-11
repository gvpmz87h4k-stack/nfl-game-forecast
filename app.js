const state = {
  data: null,
  backtest: null,
  week: null,
  filter: "all",
  selectedId: null,
  adjustments: JSON.parse(localStorage.getItem("sunday-desk-adjustments") || "{}"),
  picks: {},
  code: null,        // the private code from the personal link; sent with every save, never shown
  name: null,        // display name from the same link
  who: null,         // slug of the name: the key used in the public file and in local storage
  filePicks: null
};

function slugOf(name) { return String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "") || null; }

// A personal link looks like /?as=CODE&name=Narcisa. Opening it once tells this phone who is
// picking; the code is stored here and stripped from the address bar.
function readIdentity() {
  const params = new URLSearchParams(location.search);
  const code = params.get("as");
  const name = params.get("name");
  if (code && name) {
    try {
      localStorage.setItem("sunday-desk-code", code.trim());
      localStorage.setItem("sunday-desk-name", name.trim());
    } catch (e) { /* storage blocked */ }
    params.delete("as"); params.delete("name");
    const rest = params.toString();
    history.replaceState(null, "", `${location.pathname}${rest ? `?${rest}` : ""}${location.hash}`);
  }
  try {
    state.code = localStorage.getItem("sunday-desk-code");
    state.name = localStorage.getItem("sunday-desk-name");
  } catch (e) { /* storage blocked */ }
  state.who = slugOf(state.name);
}
readIdentity();

// Picks live on this phone under the picker's name, and are merged with the saved file on
// every load, so any phone or browser shows the same picks for the same person.
function picksKey(who) { return `sunday-desk-picks:${who}`; }

function loadLocalPicks(who) {
  if (!who) return {};
  try { return JSON.parse(localStorage.getItem(picksKey(who)) || "{}"); } catch (e) { return {}; }
}

function saveLocalPicks() {
  if (!state.who) return;
  try { localStorage.setItem(picksKey(state.who), JSON.stringify(state.picks)); } catch (e) { /* storage blocked */ }
}

function mergeFilePicks() {
  // The saved file wins over an older local copy; a local change not yet saved wins over the file.
  const source = state.filePicks && state.filePicks[state.who];
  if (!source || !state.data) return;
  const byKey = new Map(state.data.games.map(g => [`${g.week}|${g.away.abbreviation}|${g.home.abbreviation}`, g]));
  source.picks.forEach(pick => {
    const game = byKey.get(`${pick.week}|${pick.away}|${pick.home}`);
    if (!game) return;
    const local = state.picks[game.id];
    const localUnsaved = local && !local.savedAt && local.touchedAt;
    const localNewer = local && local.savedAt && pick.enteredAt && local.savedAt > pick.enteredAt;
    if (localUnsaved || localNewer) return;
    state.picks[game.id] = { winner: pick.winner || null, spread: pick.spread || null, week: game.week,
      away: game.away.abbreviation, home: game.home.abbreviation, savedAt: pick.enteredAt || "file" };
  });
  saveLocalPicks();
}


const els = {
  runStatus: document.querySelector("#runStatus"),
  dateLine: document.querySelector("#dateLine"),
  weekSelect: document.querySelector("#weekSelect"),
  gameList: document.querySelector("#gameList"),
  gameDetail: document.querySelector("#gameDetail"),
  emptyDetail: document.querySelector("#emptyDetail"),
  seasonRows: document.querySelector("#seasonRows"),
  teamSearch: document.querySelector("#teamSearch")
};

const TEAM_SIGNAL_KEYS = [
  { key: "continuity", label: "Continuity" },
  { key: "preparation", label: "Preparation" },
  { key: "lineMovement", label: "Line movement" },
  { key: "travel", label: "Travel" }
];

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const fmtPct = value => `${Math.round(value * 100)}%`;

function probabilityFromMargin(margin, uncertaintyMultiplier = 1, stdDeviation = 13.86) {
  const z = margin / (stdDeviation * uncertaintyMultiplier);
  const normalized = z / Math.SQRT2;
  const sign = normalized >= 0 ? 1 : -1;
  const abs = Math.abs(normalized);
  const t = 1 / (1 + 0.3275911 * abs);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const polynomial = (((((a5 * t) + a4) * t + a3) * t + a2) * t + a1) * t;
  const erf = sign * (1 - polynomial * Math.exp(-normalized * normalized));
  return 0.5 * (1 + erf);
}

function marketHomeWinProbability(game) {
  if (typeof game.marketHomeWinProbability === "number") return game.marketHomeWinProbability;
  const margin = Number(game.marketHomeMargin);
  if (!Number.isFinite(margin)) return 0.5;
  const config = state.data?.modelConfig || {};
  let spread = margin * (Number(config.probabilityCalibration) || 1.1);
  if (Math.abs(margin) >= (Number(config.strongFavoriteThreshold) || 9.0)) {
    spread *= Number(config.strongFavoriteScale) || 1.15;
  }
  return probabilityFromMargin(spread, 1, Number(config.marginStandardDeviation) || 13.86);
}

function seasonMap() {
  return new Map((state.data?.teams || []).map(team => [team.abbreviation, team]));
}

function formatAdjustmentValue(value, precision = 1) {
  const abs = Math.abs(value);
  if (abs < 0.0005) return "No shift";
  return `${value > 0 ? "+" : ""}${(value * 100).toFixed(precision)} pp`;
}

function computeTeamBreakdown(abbr) {
  const team = seasonMap().get(abbr);
  if (!team) return null;

  const breakdown = {
    abbreviation: abbr,
    baselineWins: Number(team.baselineWins || 0),
    previousRecord: team.previousRecord || "0-0",
    marketWins: 0,
    liveWins: 0,
    signalShifts: {},
    uncertaintyShifts: {}
  };

  TEAM_SIGNAL_KEYS.forEach(({ key }) => {
    breakdown.signalShifts[key] = 0;
  });
  breakdown.uncertaintyShifts.uncertainty = 0;

  if (!state.data?.games) return null;

  state.data.games.forEach(game => {
    const isHome = game.home.abbreviation === abbr;
    const isAway = game.away.abbreviation === abbr;
    if (!isHome && !isAway) return;

    const sign = isHome ? 1 : -1;
    const marketHome = marketHomeWinProbability(game);
    const market = isHome ? marketHome : (1 - marketHome);
    const live = isHome ? Number(game.homeWinProbability) : (1 - Number(game.homeWinProbability));
    if (Number.isFinite(market)) breakdown.marketWins += market;
    if (Number.isFinite(live)) breakdown.liveWins += live;

    TEAM_SIGNAL_KEYS.forEach(({ key }) => {
      const signal = game[key] || {};
      const shift = Number(signal.probabilityShift || 0);
      if (shift === 0 || !signal.applied) return;
      breakdown.signalShifts[key] += sign * shift;
    });

    const uncertainty = Number(game.uncertaintyProbabilityShift || 0);
    if (Number.isFinite(uncertainty)) {
      breakdown.uncertaintyShifts.uncertainty += sign * uncertainty;
    }
  });

  const signalTotal = Object.values(breakdown.signalShifts).reduce((sum, value) => sum + value, 0);
  const uncertaintyTotal = Object.values(breakdown.uncertaintyShifts).reduce((sum, value) => sum + value, 0);
  const residual = breakdown.liveWins - breakdown.marketWins - signalTotal - uncertaintyTotal;

  return {
    ...breakdown,
    marketWins: Number(breakdown.marketWins.toFixed(3)),
    liveWins: Number(breakdown.liveWins.toFixed(3)),
    signalTotal: Number(signalTotal.toFixed(4)),
    uncertaintyTotal: Number(uncertaintyTotal.toFixed(4)),
    residual: Number(residual.toFixed(4))
  };
}

function renderTeamBreakdownCard(abbr) {
  const breakdown = computeTeamBreakdown(abbr);
  if (!breakdown) return "";
  const teamMeta = seasonMap().get(abbr) || {};
  const marketProjected = Number(teamMeta.marketProjectedWins || 0) || breakdown.marketWins;
  const liveProjected = Number(teamMeta.projectedWins || 0) || breakdown.liveWins;
  const netVsMarket = liveProjected - marketProjected;

  const lines = TEAM_SIGNAL_KEYS
    .map(({ key, label }) => ({ label, value: breakdown.signalShifts[key] }))
    .filter(item => Math.abs(item.value) > 0.0005)
    .map(item => `<li><span>${item.label}</span><strong>${formatAdjustmentValue(item.value)}</strong></li>`)
    .join("");

  const uncertaintyLine = Math.abs(breakdown.uncertaintyTotal) > 0.0005
    ? `<li><span>Travel uncertainty smoothing</span><strong>${formatAdjustmentValue(breakdown.uncertaintyTotal)}</strong></li>`
    : "";

  const residualLine = Math.abs(breakdown.residual) > 0.0005
    ? `<li><span>State, rest, and manual</span><strong>${formatAdjustmentValue(breakdown.residual)}</strong></li>`
    : "";

  const logo = teamMeta.logo ? `<img src="${teamMeta.logo}" alt="">` : "";
  return `<div class="season-breakdown-card">
    <div class="season-breakdown-head"><span>${logo}${abbr}</span><small>${breakdown.previousRecord}</small></div>
    <div class="season-breakdown-stats">
      <div><span>Market implied</span><strong>${marketProjected.toFixed(1)}</strong></div>
      <div><span>Live forecast</span><strong>${liveProjected.toFixed(1)}</strong></div>
      <div><span>Net adjustment</span><strong>${formatAdjustmentValue(netVsMarket, 2)}</strong></div>
    </div>
    <ul class="season-breakdown-signals">
      ${lines || "<li><span>No active signal shifts</span><strong>No shift</strong></li>"}
      ${uncertaintyLine}
      ${residualLine}
    </ul>
  </div>`;
}

function formatKickoff(iso) {
  const date = new Date(iso);
  return {
    day: new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" }).format(date),
    time: new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date)
  };
}

function adjustedProbability(game) {
  const points = Number(state.adjustments[game.id] || 0);
  if (!points) return Number(game.homeWinProbability);
  const shift = Number(game.continuity?.probabilityShift || 0) + Number(game.preparation?.probabilityShift || 0) + Number(game.travel?.probabilityShift || 0);
  return clamp(probabilityFromMargin(Number(game.effectiveMargin || game.homeMargin || 0) + points, Number(game.travel?.uncertaintyMultiplier || 1)) + shift, 0.02, 0.98);
}

function currentGames() {
  return state.data.games.filter(game => game.week === Number(state.week));
}

// Weather that can change a game, not merely weather. Thresholds are stated in the guide.
const WEATHER_WIND_MPH = 15;
const WEATHER_COLD_F = 35;
const WEATHER_WET_CHANCE = 50;
const WEATHER_WET_WORDS = /rain|snow|sleet|storm|thunder|shower|blizzard|hail/i;

function badWeather(game) {
  const w = game.weather;
  if (!w || game.venue.indoor) return null;
  const reasons = [];
  const wind = parseFloat(w.wind);
  if (Number.isFinite(wind) && wind >= WEATHER_WIND_MPH) reasons.push(`wind ${w.wind}`);
  const low = parseFloat(String(w.temperature || "").split("-")[0]);
  if (Number.isFinite(low) && low <= WEATHER_COLD_F) reasons.push(`cold, ${w.temperature}`);
  const chance = parseFloat(w.precipitation);
  const wetWord = WEATHER_WET_WORDS.test(w.summary || "");
  if (wetWord || (Number.isFinite(chance) && chance >= WEATHER_WET_CHANCE)) reasons.push(wetWord ? String(w.summary).toLowerCase() : `${w.precipitation} chance of rain`);
  return reasons.length ? reasons.join(", ") : null;
}

function gameFlags(game) {
  const c = game.continuity || {};
  return {
    all: true,
    weather: Boolean(badWeather(game)),
    flags: Boolean(((c.away || {}).clusters || []).length || ((c.home || {}).clusters || []).length),
    close: Math.abs(adjustedProbability(game) - 0.5) <= 0.12
  };
}

function renderSummary(games) {
  document.querySelector("#gameCount").textContent = games.length;
  document.querySelector("#weatherCount").textContent = games.filter(g => gameFlags(g).weather).length;
  document.querySelector("#continuityCount").textContent = games.filter(g => gameFlags(g).flags).length;
  document.querySelector("#closeCount").textContent = games.filter(g => gameFlags(g).close).length;
}

function conditionMarkup(game) {
  const pieces = [];
  if (game.venue.indoor) pieces.push('<span class="condition-chip">Indoor</span>');
  else if (game.weather) pieces.push(`<span class="condition-chip">${game.weather.summary}, ${game.weather.temperature || "temp pending"}</span>`);
  else pieces.push('<span class="condition-chip">Weather pending</span>');
  const injuryTotal = game.away.injuries.length + game.home.injuries.length;
  if (game.continuity && game.continuity.applied) {
    const level = game.continuity.away.level === "High" || game.continuity.home.level === "High" ? "alert" : "";
    pieces.push(`<span class="condition-chip ${level}">Continuity ${game.continuity.away.score}/${game.continuity.home.score}</span>`);
  } else if (injuryTotal) pieces.push(`<span class="condition-chip">${injuryTotal} player flag${injuryTotal === 1 ? "" : "s"}</span>`);
  if (game.preparation && (game.preparation.away.items.length || game.preparation.home.items.length)) {
    const level = game.preparation.away.level === "High" || game.preparation.home.level === "High" ? "alert" : "";
    pieces.push(`<span class="condition-chip ${level}">Prep ${game.preparation.away.score}/${game.preparation.home.score}</span>`);
  }
  return pieces.join("");
}

function pickStatusText(pick) {
  const parts = [pick.winner ? `winner ${pick.winner}` : "", pick.spread ? `spread ${pick.spread}` : ""].filter(Boolean);
  if (!parts.length) return "No pick yet";
  return `${pick.savedAt ? "Saved" : "Not saved yet"}: ${parts.join(", ")}`;
}

function weekPicks(week) {
  // Walk the week's games and look each one up by id, so a pick saved in an older shape
  // (no week on it) still counts and still gets sent.
  return (state.data ? state.data.games : []).filter(g => g.week === week).map(g => {
    const p = state.picks[g.id] || {};
    return { game: g, winner: p.winner || null, spread: p.spread || null, savedAt: p.savedAt || null };
  });
}

function weekPickNote(week) {
  const all = weekPicks(week);
  const mine = all.filter(p => p.winner || p.spread);
  const unsaved = mine.filter(p => !p.savedAt).length;
  if (!mine.length) return "Tap a team on each game you want to call, then save the week once.";
  const missing = all.filter(p => !p.winner && !p.spread).map(p => `${p.game.away.abbreviation}@${p.game.home.abbreviation}`);
  return `${mine.length} of ${all.length} games picked this week${unsaved ? `, ${unsaved} not saved yet` : ", all saved"}.`
    + (missing.length ? ` Not picked: ${missing.join(", ")}.` : "")
    + " One save covers the whole week; your latest save before each kickoff counts.";
}

function renderGames() {
  const games = currentGames();
  renderSummary(games);
  els.gameList.innerHTML = "";
  const visible = games.filter(game => gameFlags(game)[state.filter]);
  const labels = { all: "Tap a tile above to narrow the list. Tap a game to open it.", weather: "Showing games with weather that can change the game.", flags: "Showing games where two or more starters from one unit may be missing.", close: "Showing games near a coin flip, a win chance between 38 and 62 percent." };
  const note = document.querySelector("#slateNote");
  if (note) note.textContent = labels[state.filter] || labels.all;
  if (!visible.length) {
    els.gameList.innerHTML = '<p class="empty-list">No games match this filter.</p>';
    return;
  }
  const template = document.querySelector("#gameTemplate");
  visible.forEach(game => {
    const node = template.content.firstElementChild.cloneNode(true);
    const kickoff = formatKickoff(game.kickoff);
    const probability = adjustedProbability(game);
    const favorite = probability >= 0.5 ? game.home : game.away;
    const favoriteProb = probability >= 0.5 ? probability : 1 - probability;
    node.dataset.id = game.id;
    node.classList.toggle("is-selected", state.selectedId === game.id);
    node.querySelector(".kickoff").innerHTML = `<strong>${kickoff.day}</strong>${kickoff.time}`;
    node.querySelector(".matchup").innerHTML = `
      <span class="team-line"><img src="${game.away.logo}" alt=""><strong>${game.away.name}</strong><small>${game.away.record}</small></span>
      <span class="team-line"><img src="${game.home.logo}" alt=""><strong>${game.home.name}</strong><small>${game.home.record}</small></span>`;
    node.querySelector(".conditions").innerHTML = conditionMarkup(game);
    node.querySelector(".pick").innerHTML = `<strong>${favorite.abbreviation}</strong><small>${fmtPct(favoriteProb)} win chance</small>`;
    node.addEventListener("click", () => selectGame(game.id));
    els.gameList.appendChild(node);
  });
}

function injuryMarkup(team) {
  if (!team.injuries.length) return '<li>No public game-status flags.</li>';
  return team.injuries.slice(0, 8).map(item => `
    <li><strong>${item.player}</strong>, ${item.position || "Player"}: ${item.status}
      <small>${item.detail || item.note || "No additional detail"}</small>
    </li>`).join("");
}

function continuityMarkup(profile, abbreviation) {
  const members = profile.clusterMembers || {};
  const roleWord = { starter: "starter", backup: "backup", unlisted: "not on the depth chart" };
  const clusters = profile.clusters.length
    ? profile.clusters.map(label => {
        const name = label.split(":")[0];
        const list = (members[name] || []).map(m => `<li><span>${m.player}</span><small>${m.position}, ${m.status}, ${roleWord[m.role] || m.role}</small></li>`).join("");
        return list ? `<details class="flag-list"><summary>${label}</summary><ul>${list}</ul></details>` : `<p>${label}</p>`;
      }).join("")
    : "<p>No multi-player communication cluster</p>";
  const setAside = (profile.discounted || []).length
    ? `<details class="flag-list flag-list-muted"><summary>${profile.discounted.length} name${profile.discounted.length === 1 ? "" : "s"} counted lightly</summary><ul>${profile.discounted.map(m => `<li><span>${m.player}</span><small>${m.position}, ${m.status}: ${m.why}</small></li>`).join("")}</ul></details>`
    : "";
  return `
    <div class="continuity-card continuity-${profile.level.toLowerCase()}">
      <div class="continuity-score"><strong>${profile.score}</strong><span>${profile.level}</span></div>
      <div><strong>${abbreviation} continuity</strong>${clusters}${setAside}</div>
    </div>`;
}

function preparationMarkup(profile, abbreviation) {
  const items = profile.items.length
    ? profile.items.map(item => {
        const counts = [];
        if (item.missedPracticeReports) counts.push(`${item.missedPracticeReports} missed-practice report${item.missedPracticeReports === 1 ? "" : "s"}`);
        if (item.limitedPracticeReports) counts.push(`${item.limitedPracticeReports} limited report${item.limitedPracticeReports === 1 ? "" : "s"}`);
        if (item.daysSinceFlag && item.decay < 1) counts.push(`${item.daysSinceFlag}d recovery decay`);
        else if (item.daysSinceFlag) counts.push(`reported ${item.daysSinceFlag}d ago`);
        return `<li><strong>${item.player}</strong>, ${item.position || "Player"}<small>${item.categories.join(", ")}${counts.length ? `; ${counts.join(", ")}` : ""}</small></li>`;
      }).join("")
    : "<li>No qualifying official preparation flags.</li>";
  const clusters = profile.clusters.length ? profile.clusters.join("; ") : "No linked-unit preparation cluster";
  return `
    <div class="preparation-card preparation-${profile.level.toLowerCase()}">
      <div class="preparation-head"><span><strong>${profile.score}</strong> ${abbreviation} readiness</span><small>${profile.level}</small></div>
      <p>${clusters}</p>
      <ul class="preparation-list">${items}</ul>
    </div>`;
}

function travelTeamMarkup(team, abbreviation) {
  return `
    <div class="travel-card">
      <div class="preparation-head"><span><strong>${team.acclimationScore.toFixed(1)}</strong> ${abbreviation} acclimation</span><small>Higher is better prepared</small></div>
      <dl class="travel-facts">
        <div><dt>Departure</dt><dd>${team.departure}<small>${team.departureStatus}</small></dd></div>
        <div><dt>Arrival</dt><dd>${team.arrival}<small>${team.arrivalStatus}</small></dd></div>
        <div><dt>Local sleep cycles</dt><dd>${team.localSleepCycles}<small>${team.sleepCyclesStatus}</small></dd></div>
        <div><dt>Local practice</dt><dd>${team.practiceLocation}<small>${team.practiceSchedule}</small></dd></div>
        <div><dt>Separate or late travel</dt><dd>${team.separateOrLateTravelers}<small>${team.travelerStatus}</small></dd></div>
        <div><dt>International experience</dt><dd>${team.internationalGames} games, ${team.internationalRecord}<small>Current coach: ${team.coachInternationalGames} game${team.coachInternationalGames === 1 ? "" : "s"}</small></dd></div>
        <div><dt>Surface familiarity</dt><dd>${team.surfaceFamiliarityLabel}<small>${team.surfaceNote}</small></dd></div>
      </dl>
    </div>`;
}

function travelMarkup(game) {
  if (!game.travel?.available) return "";
  const differential = Number(game.travel.probabilityShift || 0);
  const uncertainty = Number(game.travel.uncertaintyProbabilityShift || 0);
  const total = differential + uncertainty;
  const direction = Math.abs(total) < 0.0005 ? "No net probability adjustment" : `${total > 0 ? "+" : ""}${(total * 100).toFixed(1)} pp toward ${total > 0 ? game.home.abbreviation : game.away.abbreviation}`;
  const sources = (game.travel.sources || []).map(source => `<a href="${source.url}" target="_blank" rel="noreferrer">${source.label}</a>`).join(" | ");
  const travelSources = sources ? sources : "Sources pending";
  return `
    <div class="evidence travel-evidence">
      <div class="evidence-title-row"><h3>Travel and acclimation <small>Experimental</small></h3><span class="source-chip">${direction}</span></div>
      <div class="travel-summary">
        <span>Acclimation differential <strong>${(differential * 100).toFixed(1)} pp</strong></span>
        <span>Shared uncertainty <strong>${game.travel.uncertaintyMultiplier.toFixed(2)}x</strong></span>
        <span>Venue <strong>${game.travel.shared?.venueSurface || "Pending"}</strong></span>
      </div>
      <div class="travel-grid">
        ${travelTeamMarkup(game.travel.away, game.away.abbreviation)}
        ${travelTeamMarkup(game.travel.home, game.home.abbreviation)}
      </div>
      ${game.travel.workedOut ? `<div class="worked-out"><h4>How the app worked it out</h4><ol>${game.travel.workedOut.lines.map(l => `<li>${l}</li>`).join("")}</ol></div>` : ""}
      <p class="continuity-note">The acclimation score rewards verified local sleep opportunities, local practice, international experience, and surface familiarity. A player traveling separately changes the score only after a reliable report. The shared uncertainty pulls confidence toward 50%, rather than awarding either team points.</p>
	      <p class="travel-sources">${travelSources}</p>
	    </div>`;
}

function outsideMarkup(game) {
  const picks = Object.entries(game.outsidePicks || {});
  if (!picks.length) return "";
  const cards = picks.map(([key, pick]) => {
    const isPerson = key !== "cbs" && !key.startsWith("cbs-");
    const hidden = isPerson && !game.completed && key !== state.who;
    const text = hidden
      ? "Picked. Shown after the game."
      : `${pick.winner ? `Winner: ${pick.winner}` : "No winner pick"}${pick.spread ? ` · Spread: ${pick.spread}` : ""}`;
    return `<div class="evidence-card${hidden ? " is-hidden-pick" : ""}"><span>${pick.label || key}</span><strong>${text}</strong></div>`;
  }).join("");
  return `
      <div class="evidence">
        <div class="evidence-title-row"><h3>Outside picks <small>Entered before kickoff, graded against the close; other people&#39;s picks show after the game</small></h3></div>
        <div class="evidence-grid">${cards}</div>
      </div>`;
}

function renderDetail(game) {
  const probability = adjustedProbability(game);
  const adjustment = Number(state.adjustments[game.id] || 0);
  const kickoff = formatKickoff(game.kickoff);
  const weather = game.venue.indoor ? "Indoor, no direct weather exposure" : game.weather
    ? `${game.weather.summary}, ${game.weather.temperature || "temperature pending"}, wind ${game.weather.wind || "pending"}`
    : "Forecast not yet available";
  const shift = Number(game.continuity?.probabilityShift || 0);
  const shiftDirection = Math.abs(shift) < 0.0005 ? "No probability adjustment" : `${shift > 0 ? "+" : ""}${(shift * 100).toFixed(1)} pp toward ${shift > 0 ? game.home.abbreviation : game.away.abbreviation}`;
  const preparationShift = Number(game.preparation?.probabilityShift || 0);
  const preparationDirection = Math.abs(preparationShift) < 0.0005 ? "No probability adjustment" : `${preparationShift > 0 ? "+" : ""}${(preparationShift * 100).toFixed(1)} pp toward ${preparationShift > 0 ? game.home.abbreviation : game.away.abbreviation}`;
  const statePoints = Number(game.modelAdjustments?.teamStatePoints || 0);
  const restPoints = Number(game.modelAdjustments?.restPoints || 0);
  const signedPoints = value => `${value > 0 ? "+" : ""}${value.toFixed(1)} pts`;
  const adjustmentText = value => value === 0 ? "Neutral" : `${signedPoints(value)} toward ${value > 0 ? game.home.abbreviation : game.away.abbreviation}`;
  const rm = game.ratingModel;
  const signedMargin = value => `${value > 0 ? game.home.abbreviation : game.away.abbreviation} by ${Math.abs(value).toFixed(1)}`;
  const ratingText = rm ? `${signedMargin(rm.modelHomeMargin)}, from last season's plays alone` : "Ratings not loaded";
  const blendText = rm ? `${signedMargin(rm.blendHomeMargin)}; the market alone says ${signedMargin(game.marketHomeMargin)}` : "n/a";
  const startersText = rm ? `${game.away.abbreviation} ${rm.awayStarter || "?"} / ${game.home.abbreviation} ${rm.homeStarter || "?"}` : "n/a";
  const blindText = rm ? `mix got ${(rm.blindTest.blendAccuracy * 100).toFixed(1)}% of winners right, market alone ${(rm.blindTest.marketAccuracy * 100).toFixed(1)}%` : "n/a";
  const ratingChip = rm ? (rm.flagged ? `Disagrees with the market by ${Math.abs(rm.disagreementPoints).toFixed(1)} points, leaning ${rm.leans}` : `Agrees with the market, ${Math.abs(rm.disagreementPoints).toFixed(1)} points apart`) : "No rating";
  const kickoffPassed = new Date(game.kickoff) <= new Date();
  const savedPick = state.picks[game.id] || {};
  const yourPickStatus = kickoffPassed ? "Kickoff has passed" : pickStatusText(savedPick);
  els.emptyDetail.hidden = true;
  els.gameDetail.hidden = false;
  els.gameDetail.innerHTML = `
    <div class="detail-content">
      <div class="detail-hero">
        <div class="meta">${kickoff.day} at ${kickoff.time}<br>${game.venue.name}, ${game.venue.city}</div>
        <div class="detail-teams">
          <div class="detail-team"><img src="${game.away.logo}" alt=""><strong>${game.away.name}</strong><small>${game.away.record}</small></div>
          <span class="at">at</span>
          <div class="detail-team"><img src="${game.home.logo}" alt=""><strong>${game.home.name}</strong><small>${game.home.record}</small></div>
        </div>
        <div class="probability">
          <div class="prob-label"><span>${game.away.abbreviation} ${fmtPct(1 - probability)}</span><span>${game.home.abbreviation} ${fmtPct(probability)}</span></div>
          <div class="prob-track"><span class="away-share" style="width:${(1 - probability) * 100}%"></span><span class="home-share" style="width:${probability * 100}%"></span></div>
        </div>
      </div>
      <div class="evidence readout">
        <h3>In plain words</h3>
        ${(game.readout || []).map(line => `<p>${line}</p>`).join("")}
      </div>
      <div class="evidence">
        <h3>Game evidence</h3>
        <div class="evidence-grid">
          <div class="evidence-card"><span>Line, the head start</span><strong>${game.odds.detail || "Model only"}${game.odds.openingHomeMargin != null && Math.abs((game.odds.lineMovement || 0)) >= 0.5 ? ` (opened ${game.odds.openingHomeMargin > 0 ? game.home.abbreviation : game.away.abbreviation} by ${Math.abs(game.odds.openingHomeMargin).toFixed(1)})` : ""}</strong></div>
          <div class="evidence-card"><span>Market win probability</span><strong>${game.marketHomeWinProbability != null ? `${game.home.abbreviation} ${fmtPct(game.marketHomeWinProbability)}` : "n/a"}<small> ${game.marketProbabilitySource || ""}</small></strong></div>
          <div class="evidence-card"><span>Total, points both teams together</span><strong>${game.odds.total || "Pending"}</strong></div>
          <div class="evidence-card"><span>Conditions</span><strong>${weather}</strong></div>
          <div class="evidence-card"><span>Forecast source</span><strong>${game.probabilitySource}</strong></div>
          <div class="evidence-card"><span>Rolling team state</span><strong>${adjustmentText(statePoints)}</strong></div>
          <div class="evidence-card"><span>Rest difference</span><strong>${adjustmentText(restPoints)}</strong></div>
        </div>
      </div>
      <div class="evidence">
        <div class="evidence-title-row"><h3><a class="guide-link" href="guide.html#guide-board">Win board side-by-side</a></h3><span class="source-chip">Matchup teams</span></div>
        <div class="season-breakdown-grid">
          ${renderTeamBreakdownCard(game.away.abbreviation)}
          ${renderTeamBreakdownCard(game.home.abbreviation)}
        </div>
      </div>
      <div class="evidence">
        <div class="evidence-title-row"><h3><a class="guide-link" href="guide.html#guide-continuity">Communication continuity</a> <small>Experimental</small></h3><span class="source-chip">${game.continuity && game.continuity.applied ? shiftDirection : "Signal shown, confidence shift not applied"}</span></div>
        <div class="continuity-grid">
          ${continuityMarkup(game.continuity.away, game.away.abbreviation)}
          ${continuityMarkup(game.continuity.home, game.home.abbreviation)}
        </div>
        <p class="continuity-note">Scores measure public status inside player groups that exchange protections, assignments, and coverage calls. Lower is riskier. The model does not know private health or every starter assignment.</p>
      </div>
      <div class="evidence">
        <div class="evidence-title-row"><h3><a class="guide-link" href="guide.html#guide-preparation">Preparation disruption</a> <small>Experimental</small></h3><span class="source-chip">${game.preparation && game.preparation.applied ? preparationDirection : "Signal shown, confidence shift not applied"}</span></div>
        <div class="preparation-grid">
          ${preparationMarkup(game.preparation.away, game.away.abbreviation)}
          ${preparationMarkup(game.preparation.home, game.home.abbreviation)}
        </div>
        <p class="continuity-note">Uses official roster states and explicit public practice wording. Personal reasons are never inferred or expanded. Old flags decay after return. Travel disruption appears only when an official inactive or absence status exists.</p>
      </div>
      ${outsideMarkup(game)}
      <div class="evidence">
        <div class="evidence-title-row"><h3><a class="guide-link" href="guide.html#guide-rating">Rating model</a> <small>Second opinion, graded, never applied</small></h3><span class="source-chip">${ratingChip}</span></div>
        <div class="evidence-grid">
          <div class="evidence-card"><span>The model's own score prediction</span><strong>${ratingText}</strong></div>
          <div class="evidence-card"><span>Model mixed with the market</span><strong>${blendText}</strong></div>
          <div class="evidence-card"><span>Starting quarterbacks it assumes</span><strong>${startersText}</strong></div>
          <div class="evidence-card"><span>How it did on ${rm ? rm.blindTest.season : "2025"}, tested blind</span><strong>${blindText}</strong></div>
        </div>
        <p class="continuity-note">Opponent-adjusted expected points per play from nflverse play-by-play, with the projected starting quarterback as his own component. Fitted on 2022-2024, tested blind on 2025, where it did not beat the closing line. It is shown and graded here so the 2026 scorecard can say whether that changes.</p>
      </div>
      ${travelMarkup(game)}
      <div class="evidence">
        <h3>${game.away.abbreviation} player watch</h3>
        <ul class="injury-list">${injuryMarkup(game.away)}</ul>
      </div>
      <div class="evidence">
        <h3>${game.home.abbreviation} player watch</h3>
        <ul class="injury-list">${injuryMarkup(game.home)}</ul>
      </div>
      <div class="evidence yourpick" id="yourPick">
        <div class="evidence-title-row"><h3><a class="guide-link" href="guide.html#guide-matchup">Your pick</a> <small>Frozen at kickoff, graded with everyone else</small></h3><span class="source-chip" id="yourPickStatus">${yourPickStatus}</span></div>
        ${state.name ? `<div class="pick-row"><span>Picking as</span><strong id="whoPicks">${state.name}</strong><small>Not you? Open the app from your own link, or <button type="button" class="link-button" id="forgetIdentity">change the code</button>.</small></div>`
        : `<div class="pick-row identity-row"><span>Picking as</span><strong id="whoPicks">nobody yet</strong><small>Open the app from your personal link, or enter the code from it here.</small></div>
        <div class="pick-row identity-row">
          <input id="identityCode" type="text" inputmode="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Your code" aria-label="Your personal code">
          <input id="identityName" type="text" autocomplete="off" placeholder="Your name" aria-label="Your name">
          <button type="button" class="reset-button" id="useIdentity">Use this code</button>
        </div>`}
        <div class="pick-row"><span>Winner</span>
          <button type="button" class="pick-btn" data-kind="winner" data-team="${game.away.abbreviation}">${game.away.abbreviation}</button>
          <button type="button" class="pick-btn" data-kind="winner" data-team="${game.home.abbreviation}">${game.home.abbreviation}</button>
        </div>
        <div class="pick-row"><span>Against the spread</span>
          <button type="button" class="pick-btn" data-kind="spread" data-team="${game.away.abbreviation}">${game.away.abbreviation}</button>
          <button type="button" class="pick-btn" data-kind="spread" data-team="${game.home.abbreviation}">${game.home.abbreviation}</button>
        </div>
        <div class="pick-row"><button type="button" class="reset-button" id="sendPick">Save my Week ${game.week} picks</button><small id="pickNote">${weekPickNote(game.week)}</small></div>
      </div>
      <div class="adjuster">
        <label for="adjustmentRange"><span>Late-news adjustment toward ${game.home.abbreviation}</span><output id="adjustmentOutput">${adjustment > 0 ? "+" : ""}${adjustment.toFixed(1)} pts</output>
          <input id="adjustmentRange" type="range" min="-7" max="7" step="0.5" value="${adjustment}">
        </label>
        <p>Move left toward ${game.away.abbreviation}, right toward ${game.home.abbreviation}. Use this only for information the displayed line has not absorbed.</p>
        <button class="reset-button" id="resetAdjustment" type="button">Reset adjustment</button>
      </div>
    </div>`;
  const pickState = { winner: savedPick.winner || null, spread: savedPick.spread || null };
  const paint = () => document.querySelectorAll(".pick-btn").forEach(b => b.classList.toggle("is-on", pickState[b.dataset.kind] === b.dataset.team));
  paint();
  document.querySelectorAll(".pick-btn").forEach(b => b.addEventListener("click", () => {
    if (!state.code) { document.querySelector("#pickNote").textContent = "Open the app from your personal link first, then pick."; return; }
    if (kickoffPassed) { document.querySelector("#pickNote").textContent = "Kickoff has passed; this game is frozen."; return; }
    pickState[b.dataset.kind] = pickState[b.dataset.kind] === b.dataset.team ? null : b.dataset.team;
    paint();
    state.picks[game.id] = { winner: pickState.winner, spread: pickState.spread, week: game.week, away: game.away.abbreviation, home: game.home.abbreviation, touchedAt: new Date().toISOString(), savedAt: null };
    saveLocalPicks();
    document.querySelector("#yourPickStatus").textContent = pickStatusText(state.picks[game.id]);
    document.querySelector("#pickNote").textContent = weekPickNote(game.week);
  }));
  const useIdentity = document.querySelector("#useIdentity");
  if (useIdentity) useIdentity.addEventListener("click", () => {
    const code = document.querySelector("#identityCode").value.trim();
    const name = document.querySelector("#identityName").value.trim();
    if (!code || !name) { document.querySelector("#pickNote").textContent = "Both the code and your name are needed."; return; }
    try { localStorage.setItem("sunday-desk-code", code); localStorage.setItem("sunday-desk-name", name); } catch (e) { /* storage blocked */ }
    readIdentity();
    state.picks = loadLocalPicks(state.who);
    mergeFilePicks();
    renderGames();
    renderDetail(game);
  });
  const forget = document.querySelector("#forgetIdentity");
  if (forget) forget.addEventListener("click", () => {
    try { localStorage.removeItem("sunday-desk-code"); localStorage.removeItem("sunday-desk-name"); } catch (e) { /* storage blocked */ }
    state.code = null; state.name = null; state.who = null; state.picks = {};
    renderGames();
    renderDetail(game);
  });
  document.querySelector("#sendPick").addEventListener("click", async () => {
    const note = document.querySelector("#pickNote");
    if (!state.code) { note.textContent = "Open the app from your personal link first, then pick."; return; }
    const chosen = weekPicks(game.week).filter(p => p.winner || p.spread || state.picks[p.game.id]);
    if (!chosen.length) { note.textContent = "Tap a team on any game this week first."; return; }
    const payload = chosen.map(p => ({ away: p.game.away.abbreviation, home: p.game.home.abbreviation, winner: p.winner || "", spread: p.spread || "" }));
    const body = new URLSearchParams({ "form-name": "picks", who: state.code, week: String(game.week), picks: JSON.stringify(payload) });
    try {
      const res = await fetch("/", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
      if (!res.ok) throw new Error(`Netlify answered ${res.status}`);
      const stamp = new Date().toISOString();
      chosen.forEach(p => {
        state.picks[p.game.id] = { ...(state.picks[p.game.id] || {}), winner: p.winner, spread: p.spread, week: p.game.week,
          away: p.game.away.abbreviation, home: p.game.home.abbreviation, savedAt: stamp };
      });
      saveLocalPicks();
      const picked = chosen.filter(p => p.winner || p.spread).length;
      note.textContent = `Saved ${picked} game${picked === 1 ? "" : "s"} for Week ${game.week} as ${state.name}. They reach the ledger on the next run; each game freezes at its kickoff.`;
      document.querySelector("#yourPickStatus").textContent = pickStatusText(state.picks[game.id] || {});
    } catch (error) {
      note.textContent = `Could not save (${error.message}). On the live site this works; locally there is no form service.`;
    }
  });
  const range = document.querySelector("#adjustmentRange");
  range.addEventListener("input", event => {
    document.querySelector("#adjustmentOutput").textContent = `${event.target.value > 0 ? "+" : ""}${Number(event.target.value).toFixed(1)} pts`;
  });
  range.addEventListener("change", event => {
    state.adjustments[game.id] = Number(event.target.value);
    localStorage.setItem("sunday-desk-adjustments", JSON.stringify(state.adjustments));
    renderGames();
    renderDetail(game);
  });
  document.querySelector("#resetAdjustment").addEventListener("click", () => {
    delete state.adjustments[game.id];
    localStorage.setItem("sunday-desk-adjustments", JSON.stringify(state.adjustments));
    renderGames();
    renderDetail(game);
  });
}

function selectGame(id) {
  state.selectedId = id;
  const game = state.data.games.find(item => item.id === id);
  renderGames();
  renderDetail(game);
  if (window.innerWidth < 1050) document.querySelector("#detailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSeason() {
  const query = els.teamSearch.value.trim().toLowerCase();
  const teams = [...state.data.teams]
    .filter(team => team.name.toLowerCase().includes(query) || team.abbreviation.toLowerCase().includes(query))
    .sort((a, b) => b.projectedWins - a.projectedWins || a.name.localeCompare(b.name));
  els.seasonRows.innerHTML = teams.map(team => `
    <tr>
      <td><span class="team-cell"><img src="${team.logo}" alt="">${team.name}</span></td>
      <td>${team.previousRecord}</td>
      <td>${(team.marketProjectedWins != null ? team.marketProjectedWins : team.projectedWins).toFixed(1)}</td>
      <td class="forecast-value">${team.projectedWins.toFixed(1)}</td>
      <td>${team.range}</td>
    </tr>`).join("");
}

function renderBacktest() {
  const report = state.backtest;
  if (!report) return;
  const summary = report.summary;
  const accuracyChange = (summary.accuracy - summary.baselineAccuracy) * 100;
  const brierChange = summary.brier - summary.baselineBrier;
  const maeChange = summary.teamWinMae - summary.baselineTeamWinMae;
  const accuracyText = Math.abs(accuracyChange) < 0.01 ? "No change vs spread alone" : `${accuracyChange > 0 ? "Better" : "Worse"} by ${Math.abs(accuracyChange).toFixed(1)} percentage points`;
  const brierText = Math.abs(brierChange) < 0.0001 ? "No change vs spread alone" : `${brierChange < 0 ? "Better" : "Worse"} by ${Math.abs(brierChange).toFixed(4)}`;
  const maeText = Math.abs(maeChange) < 0.001 ? "No change vs spread alone" : `${maeChange < 0 ? "Better" : "Worse"} by ${Math.abs(maeChange).toFixed(2)} wins`;
  document.querySelector("#backtestMetrics").innerHTML = `
    <div class="backtest-metric"><strong>${(summary.accuracy * 100).toFixed(1)}%</strong><span>game winner accuracy</span><small class="${accuracyChange < 0 ? "worse" : ""}">${accuracyText}</small></div>
    <div class="backtest-metric"><strong>${summary.correctPicks}/${summary.gradedGames}</strong><span>correct winner picks</span><small>${summary.ties} tie${summary.ties === 1 ? "" : "s"} excluded</small></div>
    <div class="backtest-metric"><strong>${summary.brier.toFixed(3)}</strong><span>probability error, lower is better</span><small class="${brierChange > 0 ? "worse" : ""}">${brierText}</small></div>
    <div class="backtest-metric"><strong>${summary.teamWinMae.toFixed(2)}</strong><span>average team-win error</span><small class="${maeChange > 0 ? "worse" : ""}">${maeText}</small></div>`;
  const trainingSpan = `${report.trainingSeasons[0]}-${report.trainingSeasons.at(-1)}`;
  document.querySelector("#backtestExplainer").textContent = `Version ${report.modelVersion} was selected on ${trainingSpan} data, then frozen for this ${report.holdoutSeason} replay. Each forecast was stored before its own score was read. Earlier completed games could update rolling team state for later weeks. Score-order audit: ${report.scoreBlindVerificationPassed ? "passed" : "failed"}.`;

  const teamMap = new Map(state.data.teams.map(team => [team.abbreviation, team]));
  document.querySelector("#backtestRows").innerHTML = [...report.teams]
    .sort((a, b) => b.actualWins - a.actualWins || a.abbreviation.localeCompare(b.abbreviation))
    .map(team => {
      const meta = teamMap.get(team.abbreviation) || {};
      const errorClass = Math.abs(team.error) <= 1 ? "error-close" : team.error > 0 ? "error-positive" : "error-negative";
      const sign = value => value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
      return `<tr>
        <td><span class="team-cell">${meta.logo ? `<img src="${meta.logo}" alt="">` : ""}${meta.name || team.abbreviation}</span></td>
        <td class="forecast-value">${team.expectedWins.toFixed(1)}</td>
        <td>${team.actualWins}</td>
        <td class="${errorClass}">${sign(team.error)}</td>
        <td>${sign(team.modelSwing)}</td>
      </tr>`;
    }).join("");
}

// One row per voice, graded the same way: the market, the app, the rating model, each person,
// the CBS consensus, then each writer. Winners straight up and picks against the spread.
function renderWhoIsRight(o) {
  const box = document.querySelector("#whoIsRight");
  if (!box) return;
  const cell = (right, picks, pushes = 0) => {
    const pushText = pushes ? `<small>, ${pushes} push${pushes === 1 ? "" : "es"}</small>` : "";
    return picks ? `<strong>${right} of ${picks}</strong><small> ${Math.round((right / picks) * 100)}%</small>${pushText}` : (pushes ? `<small>no decision yet</small>${pushText}` : `<small>none yet</small>`);
  };
  const rows = [];
  rows.push({ label: "The betting market", winners: cell(o.marketCorrect, o.graded), spread: "<small>does not pick</small>", cls: "is-market" });
  rows.push({ label: "The app", winners: cell(o.modelCorrect, o.graded), spread: "<small>does not pick</small>", cls: "is-app" });
  if (o.ratingGraded) rows.push({ label: "Rating model, second opinion", winners: cell(o.ratingCorrect, o.ratingGraded), spread: o.flaggedPicks ? cell(o.flaggedCovered, o.flaggedPicks) : "<small>no flagged games yet</small>", cls: "" });
  const sources = Object.entries(o.outsideSources || {});
  const people = sources.filter(([k]) => k !== "cbs" && !k.startsWith("cbs-"));
  const consensus = sources.filter(([k]) => k === "cbs");
  const writers = sources.filter(([k]) => k.startsWith("cbs-")).sort((a, b) => a[1].label.localeCompare(b[1].label));
  [...people, ...consensus, ...writers].forEach(([key, src]) => rows.push({
    label: src.label.replace("CBS Sports experts, consensus", "CBS writers, consensus").replace("CBS Sports, ", ""),
    winners: cell(src.winnerCorrect, src.winnerPicks), spread: cell(src.spreadCovered, src.spreadPicks, src.spreadPushes),
    cls: key.startsWith("cbs") ? "is-writer" : "is-person",
  }));
  box.innerHTML = `<table class="who-table">
    <thead><tr><th scope="col">Who</th><th scope="col">Winners right</th><th scope="col">Against the spread</th></tr></thead>
    <tbody>${rows.map(r => `<tr class="${r.cls}"><td>${r.label}</td><td>${r.winners}</td><td>${r.spread}</td></tr>`).join("")}</tbody>
  </table>`;
}

function renderScorecard() {
  const card = state.data?.scorecard;
  const metrics = document.querySelector("#scorecardMetrics");
  const explainer = document.querySelector("#scorecardExplainer");
  const rows = document.querySelector("#scorecardRows");
  if (!card || !metrics) return;
  const o = card.overall || {};
  const pct = value => `${(value * 100).toFixed(1)}%`;
  if (!o.graded) {
    metrics.innerHTML = `
      <div class="backtest-metric"><strong>${card.open}</strong><span>forecasts open</span><small>refreshed on every run until kickoff</small></div>
      <div class="backtest-metric"><strong>${card.pending}</strong><span>frozen, waiting for a result</span><small>locked at kickoff</small></div>
      <div class="backtest-metric"><strong>0</strong><span>graded</span><small>the first grades arrive after Week ${state.data.currentWeek} finishes</small></div>`;
    const clv = card.closingLineValue || {};
    const clvText = clv.games ? ` Closing line value so far: ${clv.meanPoints > 0 ? "+" : ""}${clv.meanPoints} points on ${clv.games} frozen games.` : "";
    explainer.textContent = `Nothing has been graded yet. Every game's forecast is stored on each run and frozen the moment it kicks off. After the result, the frozen forecast is scored next to the market-only probability for the same game.${clvText}`;
    rows.innerHTML = "";
    return;
  }
  const accuracyDelta = (o.modelAccuracy - o.marketAccuracy) * 100;
  const accuracyText = Math.abs(accuracyDelta) < 0.05 ? "Same as the market" : `${accuracyDelta > 0 ? "Better" : "Worse"} than the market by ${Math.abs(accuracyDelta).toFixed(1)} points`;
  const brierText = Math.abs(o.brierEdge) < 0.0001 ? "Same as the market" : `${o.brierEdge > 0 ? "Better" : "Worse"} than the market by ${Math.abs(o.brierEdge).toFixed(4)}`;
  if (o.modelCorrect == null) o.modelCorrect = Math.round((o.modelAccuracy || 0) * o.graded);
  if (o.ratingCorrect == null && o.ratingGraded) o.ratingCorrect = Math.round((o.ratingAccuracy || 0) * o.ratingGraded);
  metrics.innerHTML = `
    <div class="backtest-metric"><strong>${pct(o.modelAccuracy)}</strong><span>model winner accuracy, ${o.graded} graded</span><small class="${accuracyDelta < 0 ? "worse" : ""}">${accuracyText}</small></div>
    <div class="backtest-metric"><strong>${pct(o.marketAccuracy)}</strong><span>market-only accuracy</span><small>${o.marketCorrect}/${o.graded} picks</small></div>
    <div class="backtest-metric"><strong>${o.modelBrier.toFixed(4)}</strong><span>model probability error, lower is better</span><small class="${o.brierEdge < 0 ? "worse" : ""}">${brierText}</small></div>
    <div class="backtest-metric"><strong>${o.modelWonDisagreements}/${o.picksDiffered}</strong><span>disagreements with the market the model won</span><small>${card.pending} frozen, ${card.late} seen late and excluded</small></div>`;
  const clv = card.closingLineValue || {};
  const clvText = clv.games
    ? `Closing line value: on ${clv.games} frozen games the closing line moved an average of ${clv.meanPoints > 0 ? "+" : ""}${clv.meanPoints} points toward the model's first-seen pick (${Math.round((clv.positiveShare || 0) * 100)}% positive); on the ${clv.disagreements} games where the model disagreed with the early line, ${clv.disagreementMeanPoints == null ? "no data" : `${clv.disagreementMeanPoints > 0 ? "+" : ""}${clv.disagreementMeanPoints} points`}.`
    : "Closing line value arrives once frozen games have both a first-seen line and a close.";
  const acc = card.acclimation || {};
  const accText = acc.games
    ? ` Long-trip games where one side slept ${acc.gapNights} or more nights longer on local time: the better-rested side covered ${acc.covered} of ${acc.decided}${acc.pushes ? `, ${acc.pushes} push${acc.pushes === 1 ? "" : "es"}` : ""} (${acc.list.map(g => `${g.away}@${g.home}, ${g.restedSide} by ${Math.abs(g.gap)} nights${g.push ? ", push" : g.covered == null ? ", pending" : g.covered ? ", covered" : ", did not"}`).join("; ")}).`
    : " No long-trip game with a five-night acclimation gap has been played yet.";
  explainer.textContent = `Every forecast was frozen at kickoff and graded after the result, with the market-only probability scored on the same games. Ties are excluded; ${o.ties || 0} so far. ${clvText}${accText}`;
  renderWhoIsRight(o);
  rows.innerHTML = card.weeks.filter(week => week.graded).map(week => `
    <tr>
      <td>Week ${week.week}</td>
      <td>${week.graded}</td>
      <td class="forecast-value">${pct(week.modelAccuracy)}</td>
      <td>${pct(week.marketAccuracy)}</td>
      <td>${week.modelBrier.toFixed(4)}</td>
      <td>${week.marketBrier.toFixed(4)}</td>
      <td class="${week.brierEdge < 0 ? "error-negative" : "error-close"}">${week.brierEdge > 0 ? "+" : ""}${week.brierEdge.toFixed(4)}</td>
    </tr>`).join("");
}

function setup(data) {
  state.data = data;
  state.week = data.currentWeek;
  state.picks = loadLocalPicks(state.who);
  mergeFilePicks();
  applyData(data);
}

// Re-fetch the numbers whenever the app comes back to the front, and every ten minutes while it
// stays there. A home screen app keeps its last screen alive between uses, so without this the
// stamp could sit on an old look until the app was closed for good.
let refreshing = false;
async function refreshData() {
  if (refreshing || !state.data || document.visibilityState === "hidden") return;
  refreshing = true;
  try {
    const [snapshot, filePicks] = await Promise.all([loadData("snapshot.json"), loadPicksFile().catch(() => null)]);
    if (filePicks) state.filePicks = filePicks;
    if (snapshot.json.updatedAt !== state.data.updatedAt) {
      state.data = snapshot.json;
      state.dataSource = snapshot.source;
      if (!state.data.games.some(g => g.week === state.week)) state.week = state.data.currentWeek;
      mergeFilePicks();
      applyData(state.data);
      const selected = state.selectedId && state.data.games.find(g => g.id === state.selectedId);
      if (selected) renderDetail(selected);
    }
  } catch (error) { /* keep what is on screen; the next wake tries again */ }
  refreshing = false;
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshData(); });
window.addEventListener("focus", refreshData);
window.addEventListener("pageshow", event => { if (event.persisted) refreshData(); });
setInterval(refreshData, 10 * 60 * 1000);

function applyData(data) {
  const weeks = [...new Set(data.games.map(game => game.week))].sort((a, b) => a - b);
  els.weekSelect.innerHTML = weeks.map(week => `<option value="${week}" ${week === state.week ? "selected" : ""}>Week ${week}</option>`).join("");
  const updated = new Date(data.updatedAt);
  els.runStatus.classList.add("is-current");
  const stampDate = new Intl.DateTimeFormat("en-US", { month: "numeric", day: "numeric" }).format(updated);
  const stampTime = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(updated);
  els.runStatus.querySelector("span:last-child").innerHTML = `<span class="status-word">Updated </span>${stampDate} @ ${stampTime}`;
  els.dateLine.textContent = `${data.season} regular season, Week ${state.week}`;
  renderGames();
  renderSeason();
  renderScorecard();
  renderBacktest();
}

els.weekSelect.addEventListener("change", event => {
  state.week = Number(event.target.value);
  state.selectedId = null;
  els.gameDetail.hidden = true;
  els.emptyDetail.hidden = false;
  els.dateLine.textContent = `${state.data.season} regular season, Week ${state.week}`;
  renderGames();
});

document.querySelectorAll(".pulse-tile").forEach(tile => tile.addEventListener("click", () => {
  state.filter = tile.dataset.filter;
  document.querySelectorAll(".pulse-tile").forEach(item => item.setAttribute("aria-pressed", String(item === tile)));
  renderGames();
}));

els.teamSearch.addEventListener("input", renderSeason);

// Data lives in the GitHub repository, where the scheduled job commits it twice a day. Reading it
// from there means Netlify never has to redeploy for numbers; the copy bundled with the deploy
// is only the fallback for when GitHub cannot be reached.
const DATA_ORIGIN = "https://raw.githubusercontent.com/gvpmz87h4k-stack/nfl-game-forecast/main/";
async function loadData(name) {
  const stamp = Date.now();
  try {
    const fresh = await fetch(`${DATA_ORIGIN}data/${name}?t=${stamp}`, { cache: "no-store" });
    if (fresh.ok) return { json: await fresh.json(), source: "github" };
  } catch (error) { /* fall through to the deployed copy */ }
  const local = await fetch(`data/${name}`, { cache: "no-store" });
  if (!local.ok) throw new Error(`${name} returned ${local.status}`);
  return { json: await local.json(), source: "deploy" };
}

async function loadPicksFile() {
  const res = await fetch(`${DATA_ORIGIN}picks.json?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`picks.json returned ${res.status}`);
  return (await res.json()).sources || {};
}

Promise.all([loadData("snapshot.json"), loadData("backtest-2025.json").catch(() => null), loadPicksFile().catch(() => null)])
  .then(([snapshot, backtest, filePicks]) => {
    state.filePicks = filePicks;
    if (backtest) state.backtest = backtest.json;
    state.dataSource = snapshot.source;
    return snapshot.json;
  })
  .then(setup)
  .catch(error => {
    els.runStatus.querySelector("span:last-child").textContent = "Snapshot unavailable";
    els.gameList.innerHTML = `<p class="empty-list">Run the updater, then reload this page. ${error.message}</p>`;
  });
