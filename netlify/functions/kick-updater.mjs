// Starts the GitHub updater job on time.
//
// GitHub's own schedule runs hours late when it is busy, but a run asked for by hand starts
// within seconds. Netlify's scheduler is punctual, so this function wakes every 15 minutes,
// works out the time in Chicago (daylight time included, so no twin slots), and when the
// current quarter hour is one of the wanted looks it asks GitHub to start the workflow.
//
// Needs GITHUB_DISPATCH_TOKEN in the site environment variables, scope Functions or All: a fine-grained token for
// this one repository with Actions set to read and write. Without it the function only logs.

const REPO = "gvpmz87h4k-stack/nfl-game-forecast";
const WORKFLOW = "update.yml";

// Chicago time, quarter hours. Kickoffs: noon, 3:25, and 7:15 to 8:20 at night; inactive
// lists arrive 90 minutes before each kickoff.
export const SLOTS = [
  { days: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], time: "10:15" },  // morning look
  { days: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], time: "17:15" },  // evening look, before night inactives
  { days: ["Sun"], time: "08:15" },                                          // morning games played abroad
  { days: ["Sun"], time: "11:45" },                                          // after the noon games' inactive lists
  { days: ["Sun"], time: "14:45" },                                          // after the 3:25 games' inactive lists
  { days: ["Sun", "Mon", "Thu"], time: "18:15" },                            // after the night game's inactive list
  { days: ["Wed"], time: "18:15" },                                          // Week 1 opener is a Wednesday
  { days: ["Tue"], time: "19:30" },                                          // one-night proof slot, remove after
  { days: ["Tue"], time: "19:45" },                                          // one-night proof slot, remove after
  { days: ["Tue"], time: "20:00" },                                          // one-night proof slot, remove after
];

export function chicagoNow(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const get = type => parts.find(p => p.type === type)?.value;
  const hour = get("hour") === "24" ? "00" : get("hour");
  return { day: get("weekday"), hour, minute: Number(get("minute")) };
}

export function wantedSlot(date = new Date(), slots = SLOTS) {
  const now = chicagoNow(date);
  const quarter = String(Math.floor(now.minute / 15) * 15).padStart(2, "0");
  const stamp = `${now.hour}:${quarter}`;
  return slots.find(slot => slot.time === stamp && slot.days.includes(now.day)) || null;
}

export async function dispatch(token) {
  const response = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "sunday-desk-kick",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  return response.status;
}

export default async () => {
  const slot = wantedSlot();
  const { day, hour, minute } = chicagoNow();
  if (!slot) {
    console.log(`kick-updater: ${day} ${hour}:${String(minute).padStart(2, "0")} Chicago, not a look; nothing to do.`);
    return new Response("idle", { status: 200 });
  }
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  if (!token) {
    console.log(`kick-updater: ${day} ${slot.time} Chicago is a look, but GITHUB_DISPATCH_TOKEN is not set.`);
    return new Response("no token", { status: 200 });
  }
  const status = await dispatch(token);
  console.log(`kick-updater: ${day} ${slot.time} Chicago, asked GitHub to run the updater; GitHub answered ${status}.`);
  return new Response(String(status), { status: 200 });
};

export const config = { schedule: "*/15 * * * *" };

// Redeploy marker: variables are baked in at build time, so a redeploy follows any change to the key.
