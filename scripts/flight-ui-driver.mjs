// Observes the real served GroundStation UI through a full simulated flight.
// Self-driven: polls the page once a second, records a JSONL timeline, takes a
// screenshot at every mission-phase change and notable event, and writes
// ui-result.json with pass/fail checks when the controller marks the run done
// (EVIDENCE_DIR/status.json {"done":true}) or UI_TIMEOUT_S elapses.
//
// Env: GS_ORIGIN, GS_USER, GS_PASSWORD, EVIDENCE_DIR, PLAYWRIGHT_MODULE,
//      CHROME_BINARY (optional), UI_TIMEOUT_S (default 7200).
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const moduleName = process.env.PLAYWRIGHT_MODULE || 'playwright';
const {chromium} = await import(moduleName.startsWith('/') ? pathToFileURL(moduleName).href : moduleName);
const origin = process.env.GS_ORIGIN;
const out = path.join(process.env.EVIDENCE_DIR || '.', 'ui');
fs.mkdirSync(out, {recursive: true});
const timeline = fs.createWriteStream(path.join(out, 'ui-timeline.jsonl'), {flags: 'a'});
const deadline = Date.now() + 1000 * Number(process.env.UI_TIMEOUT_S || 7200);
const log = (event, data = {}) => timeline.write(JSON.stringify({wall_ms: Date.now(), event, ...data}) + '\n');

const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CHROME_BINARY || undefined,
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const context = await browser.newContext({viewport: {width: 1500, height: 1000}});
// The user setting under test: switch to Map after landing.
await context.addInitScript(() => { try { localStorage.setItem('gs_map_after_landing', 'on'); } catch {} });
const page = await context.newPage();
let shotIndex = 0;
const shot = async name => {
  const file = `${String(++shotIndex).padStart(2, '0')}-${name}.png`;
  await page.screenshot({path: path.join(out, file)}).catch(() => {});
  log('screenshot', {file});
  return file;
};

async function login() {
  const user = page.locator('#gs26-login-username');
  if (await user.isVisible().catch(() => false)) {
    await user.fill(process.env.GS_USER);
    await page.locator('#gs26-login-password').fill(process.env.GS_PASSWORD);
    await page.getByRole('button', {name: 'Sign In', exact: true}).click();
  }
}
async function openDashboard() {
  const nav = page.getByRole('navigation', {name: 'Mission navigation'});
  const link = nav.getByRole('link', {name: 'Dashboard', exact: true});
  if (await link.isVisible().catch(() => false)) await link.click({timeout: 5000}).catch(() => {});
}

async function sample() {
  return page.evaluate(() => {
    const phaseEl = document.querySelector('.gs26-status-phase');
    const flight = document.querySelector('.gs26-status-flight');
    const dash = document.querySelector('section.gs26-custom-dashboard');
    const cards = dash ? [...dash.querySelectorAll('.gs26-dashboard-card')].map(c => c.dataset.cardId) : [];
    const map = document.querySelector('[id^="gs26-mini-map-"]');
    const ground = document.getElementById('ground-map');
    const groundVisible = !!ground && ground.getBoundingClientRect().width > 0;
    const text = document.body.innerText || '';
    const notes = (text.match(/(Drogue|Main) parachute deployed[^\n]*|FC contact (lost|restored)[^\n]*/g) || []);
    const maxima = [...document.querySelectorAll('.gs26-maxima-card')].map(c => c.innerText.replace(/\s+/g, ' '));
    return {
      phase: phaseEl ? phaseEl.dataset.phase : null,
      phase_text: phaseEl ? phaseEl.innerText : null,
      flight_text: flight ? flight.innerText.replace(/\s+/g, ' ') : null,
      profile: dash ? dash.dataset.phaseProfile : null,
      dashboard_visible: !!dash && dash.getBoundingClientRect().width > 0,
      first_cards: cards.slice(0, 12),
      mini_map: map ? {lat: map.dataset.rocketLat, lon: map.dataset.rocketLon, updates: map.dataset.updates,
        state: map.dataset.miniMapState, visible: map.dataset.miniMapVisible,
        markers: [...map.querySelectorAll('.gs26-mini-map-marker')].map(m => m.dataset.marker)} : null,
      map_tab_visible: groundVisible,
      notes, maxima,
    };
  });
}

const checks = {
  phases_seen: [], profile_switch_in_flight: false, flight_cards_on_top: false,
  drogue_notification: null, main_notification: null, mini_map_positions: new Set(),
  comms_loss_landed_seen: false, comms_loss_reverted: false, map_switch_after_landing: 0,
  maxima_text: [],
};

await page.goto(origin + '/dashboard');
await page.waitForTimeout(1500);
await login();
await page.waitForTimeout(2000);
await openDashboard();
await shot('start');
log('start', {origin});

let last = {};
let lastLanded = false;
const done = () => {
  try {
    const status = JSON.parse(fs.readFileSync(process.env.STATUS_FILE || path.join(process.env.EVIDENCE_DIR, 'status.json'), 'utf8'));
    return status.done === true || ['done', 'finished', 'complete', 'failed'].includes(status.step);
  }
  catch { return false; }
};
while (Date.now() < deadline && !done()) {
  let s;
  try { s = await sample(); } catch (e) { log('sample_error', {error: String(e).slice(0, 200)}); await page.waitForTimeout(1000); continue; }
  if (s.phase && s.phase !== last.phase) {
    if (!checks.phases_seen.includes(s.phase)) checks.phases_seen.push(s.phase);
    log('phase', s);
    await shot(`phase-${s.phase}${s.phase_text && s.phase_text.includes('contact lost') ? '-comms-loss' : ''}`);
  }
  if (s.phase === 'in_flight' && s.profile === 'in_flight') {
    if (!checks.profile_switch_in_flight) { checks.profile_switch_in_flight = true; log('profile_in_flight', s); await shot('dashboard-in-flight-profile'); }
    const top = s.first_cards.slice(0, 6);
    if (['flight-altitude', 'flight-speed', 'flight-accel'].every(id => top.includes(id))) checks.flight_cards_on_top = true;
  }
  for (const note of s.notes) {
    if (note.startsWith('Drogue') && !checks.drogue_notification) { checks.drogue_notification = note; log('notification', {note}); await shot('notification-drogue'); }
    if (note.startsWith('Main') && !checks.main_notification) { checks.main_notification = note; log('notification', {note}); await shot('notification-main'); }
  }
  if (s.mini_map && s.mini_map.lat) {
    const key = `${s.mini_map.lat},${s.mini_map.lon}`;
    if (!checks.mini_map_positions.has(key)) { checks.mini_map_positions.add(key); log('mini_map', s.mini_map); if (checks.mini_map_positions.size === 2) await shot('mini-map-moved'); }
  }
  const commsLost = !!(s.phase_text && s.phase_text.includes('contact lost'));
  if (commsLost && !checks.comms_loss_landed_seen) { checks.comms_loss_landed_seen = true; log('comms_loss_landed', s); }
  if (checks.comms_loss_landed_seen && !commsLost && s.phase === 'in_flight' && !checks.comms_loss_reverted) { checks.comms_loss_reverted = true; log('comms_loss_reverted', s); await shot('comms-loss-reverted'); }
  const landed = s.phase === 'landed';
  if (landed && !lastLanded) {
    // Give the UI a moment to apply the post-landing Map switch.
    await page.waitForTimeout(2500);
    const after = await sample().catch(() => s);
    if (after.map_tab_visible && !after.dashboard_visible) { checks.map_switch_after_landing += 1; log('map_switch', after); await shot('map-after-landing'); }
    else log('no_map_switch', after);
    // Return to the dashboard so later phases stay observable.
    await openDashboard();
    await page.waitForTimeout(1000);
  }
  lastLanded = landed;
  if (s.maxima.length) checks.maxima_text = s.maxima;
  last = s;
  await page.waitForTimeout(1000);
}

// Final evidence: dashboard with maxima, Notifications history, Detailed tab.
await openDashboard();
await page.waitForTimeout(1500);
await shot('final-dashboard');
const nav = page.getByRole('navigation', {name: 'Mission navigation'});
for (const tab of ['Notifications', 'Detailed']) {
  const link = nav.getByRole('link', {name: tab, exact: true});
  if (await link.isVisible().catch(() => false)) {
    await link.click().catch(() => {});
    await page.waitForTimeout(1500);
    await shot(`final-${tab.toLowerCase()}`);
    const text = await page.locator('body').innerText().catch(() => '');
    log(`final_${tab.toLowerCase()}`, {text: text.slice(0, 4000)});
    if (tab === 'Notifications') {
      checks.drogue_notification ||= (text.match(/Drogue parachute deployed[^\n]*/) || [null])[0];
      checks.main_notification ||= (text.match(/Main parachute deployed[^\n]*/) || [null])[0];
    }
  }
}
const result = {
  ...checks,
  mini_map_positions: [...checks.mini_map_positions],
  pass: {
    phase_labels: ['startup', 'idle', 'fill', 'launch', 'in_flight', 'landed'].every(p => checks.phases_seen.includes(p)),
    drogue_notification: !!checks.drogue_notification,
    main_notification: !!checks.main_notification,
    in_flight_profile: checks.profile_switch_in_flight && checks.flight_cards_on_top,
    mini_map_moved: checks.mini_map_positions.size >= 2,
    comms_loss_landed_and_reverted: checks.comms_loss_landed_seen && checks.comms_loss_reverted,
    map_switch_after_landing: checks.map_switch_after_landing >= 1,
  },
};
fs.writeFileSync(path.join(out, 'ui-result.json'), JSON.stringify(result, null, 2));
log('done', result.pass);
await browser.close();
process.exit(Object.values(result.pass).every(Boolean) ? 0 : 1);
