// The kitchen scene: everything is drawn on a 480x270 logical canvas and the
// browser scales the whole canvas by an integer factor with CSS
// `image-rendering: pixelated`. One logical pixel stays one (fat, crisp)
// pixel at any size — sprites cannot smear because there are no sprites to
// smear: the art is drawn per-pixel at this resolution, right here.

export const W = 480;
export const H = 270;

// Palette: warm kitchen — charcoal-plum room, ember burners, cream plates,
// cyan steam. Deliberately not NES grey: the room should look *lived in*.
export const PAL = {
  bg: '#241c2e',        // charcoal-plum wall
  bgShade: '#1c1624',
  wall: '#322640',
  trim: '#4a3a5e',
  window: '#123047',    // night glass
  windowDay: '#3f6f8f',
  windowFrame: '#5d4b73',
  counter: '#5a4436',
  counterEdge: '#3c2d24',
  counterTop: '#7a5c48',
  tile: '#3a2c4a',
  tileLine: '#2c2138',
  metal: '#8f8fa8',
  metalDark: '#5c5c74',
  pot: '#6b6b85',
  potDark: '#47475e',
  potRim: '#a8a8c2',
  lid: '#9a8fc0',
  ember: '#ff9f43',     // burner ember amber
  emberHot: '#ffd166',
  flame: '#ff7b2e',
  flameCore: '#ffe08a',
  cream: '#f2e7cf',     // plates
  creamShade: '#cbbfa5',
  steam: '#9fe8ea',     // cyan steam
  smoke: '#6e6e7e',
  red: '#e05252',
  green: '#6fcf7c',
  amber: '#ffb347',
  ink: '#160f1e',
  paper: '#efe6d2',
  tape: '#e8d98a',
  text: '#f7f3e8',
  dim: '#8a7f9a',
};

const GEN_ORDER = ['research', 'deep-dive', 'project-review', 'homelab-audit',
  'brainstorm', 'creation'];

const HEAT_ENERGY = { filling: 0.35, simmer: 0.62, boiling: 1.0 };

function potBox(i) {
  // Six burners spread across the counter. Pot ~40px wide, generous gaps for
  // labels — a crowded stove reads as broken at 480 logical px.
  const x = 26 + i * 74;
  const y = 178;
  return { x, y, w: 44, h: 34 };
}

function px(ctx, x, y, c) {
  ctx.fillStyle = c;
  ctx.fillRect(x | 0, y | 0, 1, 1);
}

function rect(ctx, x, y, w, h, c) {
  ctx.fillStyle = c;
  ctx.fillRect(x | 0, y | 0, w | 0, h | 0);
}

function text(ctx, s, x, y, c = PAL.text, size = 8) {
  ctx.fillStyle = c;
  ctx.font = `${size}px "Press Start 2P", monospace`;
  ctx.fillText(s, x | 0, y | 0);
}

function drawRoom(ctx, st, t) {
  rect(ctx, 0, 0, W, H, PAL.bg);
  // Wall tiles, faint, so the plum wall is not a flat swatch.
  for (let x = 0; x < W; x += 16) rect(ctx, x, 96, 1, 66, PAL.tileLine);
  for (let y = 96; y < 162; y += 16) rect(ctx, 0, y, W, 1, PAL.tileLine);
  rect(ctx, 0, 96, W, 2, PAL.trim);

  // Window: the sky IS the bus state. Daylight when the wire is ours,
  // night-rain when someone else is generating — the cheapest honest signal
  // the room has.
  const busy = st.busy;
  rect(ctx, 24, 20, 120, 64, PAL.windowFrame);
  rect(ctx, 28, 24, 112, 56, busy ? PAL.window : PAL.windowDay);
  if (!busy) {
    // sun
    rect(ctx, 60, 36, 14, 14, PAL.emberHot);
    rect(ctx, 56, 40, 22, 6, PAL.emberHot);
    rect(ctx, 64, 32, 6, 22, PAL.emberHot);
  } else {
    // moon + rain
    rect(ctx, 58, 34, 12, 12, PAL.cream);
    rect(ctx, 54, 36, 8, 8, PAL.window);
    for (let i = 0; i < 7; i++) {
      const rx = 32 + ((i * 15 + ((t * 60) | 0)) % 104);
      const ry = 44 + ((i * 9 + ((t * 140) | 0)) % 30);
      px(ctx, rx, ry, PAL.steam);
      px(ctx, rx, ry + 2, PAL.steam);
    }
  }

  // Wall clock with a seconds hand that actually ticks: proof the page is
  // alive without a spinner.
  const now = new Date();
  rect(ctx, 420, 24, 36, 36, PAL.wall);
  rect(ctx, 422, 26, 32, 32, PAL.paper);
  rect(ctx, 437, 27, 2, 3, PAL.ink);
  const sec = now.getSeconds() + now.getMilliseconds() / 1000;
  const a = sec * Math.PI * 2 / 60 - Math.PI / 2;
  px(ctx, 438 + Math.round(Math.cos(a) * 10), 42 + Math.round(Math.sin(a) * 10), PAL.red);
  rect(ctx, 438, 42, 1, 1, PAL.ink);

  // Hood + shelf with the day's plates stacked (plated count, honest).
  rect(ctx, 200, 30, 176, 6, PAL.metalDark);
  rect(ctx, 204, 36, 168, 4, PAL.trim);
  rect(ctx, 208, 52, 160, 5, PAL.counterEdge);   // shelf
  const plates = st.platedToday || 0;
  for (let i = 0; i < Math.min(plates, 5); i++) {
    rect(ctx, 216 + i * 30, 46, 20, 3, PAL.cream);
    rect(ctx, 218 + i * 30, 44, 16, 2, PAL.creamShade);
  }
  text(ctx, `${plates} today`, 296, 44 + 14, PAL.dim, 8);

  // Counter
  rect(ctx, 0, 162, W, 8, PAL.counterTop);
  rect(ctx, 0, 170, W, 2, PAL.counterEdge);
  rect(ctx, 0, 172, W, 74, PAL.counter);
  for (let x = 8; x < W; x += 48) rect(ctx, x, 178, 1, 64, PAL.counterEdge);
  rect(ctx, 0, 246, W, 24, PAL.bgShade);

  // Digest plate — the morning paper, under the pass, center-right.
  const d = st.digestWritten;
  rect(ctx, 396, 196, 56, 8, PAL.creamShade);
  rect(ctx, 400, 188, 48, 10, d ? PAL.paper : PAL.metalDark);
  if (d) text(ctx, 'DIG', 410, 196, PAL.ink, 8);

  // Stove line: burners under every pot position
  for (let i = 0; i < 6; i++) {
    const b = potBox(i);
    rect(ctx, b.x - 2, 174, b.w + 4, 4, PAL.metalDark);
  }
}

function drawBurner(ctx, b, energy, t, seed) {
  if (energy <= 0) {
    rect(ctx, b.x + 4, 172, b.w - 8, 2, PAL.metalDark);
    return;
  }
  const hot = PAL.ember, hotter = PAL.emberHot;
  for (let x = b.x + 4; x < b.x + b.w - 4; x += 2) {
    const flick = ((Math.sin(t * 18 + x + seed) + 1) / 2);
    const hgt = 2 + Math.round(flick * energy * 5);
    rect(ctx, x, 172 - hgt, 1, hgt, flick > 0.7 ? hotter : hot);
  }
}

function drawPot(ctx, pot, b, t, events) {
  const s = pot.state;
  const energy = HEAT_ENERGY[s] || 0;
  const taped = s === 'taped';
  const preempted = s === 'preempted';
  const { x, y, w, h } = b;

  // pot body
  rect(ctx, x, y, w, h, taped ? PAL.metalDark : PAL.pot);
  rect(ctx, x + 2, y + 2, w - 4, h - 6, taped ? PAL.potDark : PAL.potDark);
  rect(ctx, x - 2, y - 2, w + 4, 3, PAL.potRim);    // rim
  rect(ctx, x - 4, y + 8, 4, 4, PAL.metal);          // handles
  rect(ctx, x + w, y + 8, 4, 4, PAL.metal);
  // contents shimmer when hot
  if (energy > 0 && !taped) {
    rect(ctx, x + 3, y + 1, w - 6, 2, PAL.ember);
  }

  // lid: off when boiling so the steam can leave; askew on filling
  if (s === 'boiling') {
    rect(ctx, x + w - 14, y - 12, 14, 3, PAL.lid);
    rect(ctx, x + w - 8, y - 15, 3, 3, PAL.lid);
  } else if (s === 'filling') {
    rect(ctx, x + 2, y - 5, w - 6, 3, PAL.lid);
    rect(ctx, x + w / 2 - 1, y - 8, 3, 3, PAL.lid);
  } else if (!preempted && !taped) {
    rect(ctx, x + 1, y - 6, w - 2, 3, PAL.lid);
    rect(ctx, x + w / 2 - 1, y - 9, 3, 3, PAL.lid);
  }

  // flames
  if (energy > 0 && !taped) drawBurner(ctx, b, energy, t, x);

  // ingredients dropping in while filling
  if (s === 'filling') {
    for (let i = 0; i < 3; i++) {
      const fx = x + 8 + i * 12 + Math.round(Math.sin(t * 3 + i * 2) * 2);
      const fy = y - 24 + ((t * 40 + i * 13) % 24);
      px(ctx, fx, fy, PAL.green);
      px(ctx, fx + 1, fy + 1, PAL.green);
    }
  }

  // bubbles: count and size scale with heat. The boil has to *look* busy.
  if (energy > 0 && !taped) {
    const n = Math.round(energy * 7);
    for (let i = 0; i < n; i++) {
      const bx = x + 5 + ((i * 13 + ((t * (20 + energy * 60)) | 0)) % (w - 10));
      const by = y + 3 + ((i * 7) % 8);
      const r = energy > 0.8 ? 2 : 1;
      rect(ctx, bx, by - ((t * 30 + i * 5) % 4), r, r, PAL.steam);
    }
  }

  // steam plumes above boiling/simmering pots
  if ((s === 'boiling' || s === 'simmer') && !taped) {
    const n = s === 'boiling' ? 4 : 2;
    for (let i = 0; i < n; i++) {
      const rise = (t * 26 + i * 9) % 34;
      const sx = x + w / 2 + Math.round(Math.sin(t * 4 + i * 2.1) * 5) + (i - 1) * 6;
      const sy = y - 10 - rise;
      const a = 1 - rise / 34;
      if (a > 0.5) { px(ctx, sx, sy, PAL.steam); px(ctx, sx + 1, sy - 2, PAL.steam); }
      else { px(ctx, sx, sy, '#5f979a'); }
    }
  }

  // preempted: grey smoke puff + BACKOFF countdown, the centrepiece of the
  // whole politeness story
  if (preempted) {
    for (let i = 0; i < 5; i++) {
      const rise = (t * 16 + i * 7) % 26;
      const sx = x + 8 + i * 7 + Math.round(Math.sin(t * 2 + i) * 3);
      const sy = y - 6 - rise;
      rect(ctx, sx, sy, 3, 2, PAL.smoke);
    }
    if (pot.backoff_s > 0) {
      const label = `BACKOFF ${Math.ceil(pot.backoff_s)}s`;
      rect(ctx, x - 14, y + h + 2, 72, 12, PAL.ink);
      text(ctx, label, x - 12, y + h + 11, PAL.amber, 8);
    }
  }

  // taped off: crossed tape, scheduled but deliberately not cooking
  if (taped) {
    ctx.fillStyle = PAL.tape;
    for (let i = -w; i < w; i++) px(ctx, x + i, y + h / 2 - i / 2.2, PAL.tape);
    for (let i = -w; i < w; i++) px(ctx, x + w + i, y + h / 2 + i / 2.2, PAL.tape);
    text(ctx, 'OFF', x + 10, y + h - 6, PAL.ink, 8);
  }

  // swept: the broom that took the rejected plate away
  if (s === 'swept') {
    rect(ctx, x + 4, y - 14, 3, 12, '#a5713c');
    rect(ctx, x + 1, y - 4, 9, 6, PAL.creamShade);
  }

  // plated: a cloche slides in beside the pot — click to read it
  if (s === 'plated' || (pot.today && pot.today.plated > 0)) {
    const glow = (Math.sin(t * 3) + 1) / 2;
    rect(ctx, x + 2, y - 24, 18, 3, PAL.cream);
    rect(ctx, x + 4, y - 28, 14, 4, glow > 0.5 ? PAL.emberHot : PAL.cream);
    rect(ctx, x + 10, y - 31, 2, 3, PAL.creamShade);
  }

  // waiting: just the generator tag, quiet
  if (s === 'waiting') text(ctx, '~', x + w - 8, y - 10, PAL.dim, 8);

  // progress pips under the burner (chain stages done/seen)
  if (pot.progress) {
    const { done, seen } = pot.progress;
    for (let i = 0; i < seen; i++) {
      rect(ctx, x + i * 6, 176, 4, 2, i < done ? PAL.green : PAL.metalDark);
    }
  }

  // label
  const label = pot.generator === 'project-review' ? 'review'
    : pot.generator === 'homelab-audit' ? 'audit'
    : pot.generator === 'deep-dive' ? 'dive'
    : pot.generator === 'brainstorm' ? 'ideas'
    : pot.generator === 'creation' ? 'create'
    : 'search';
  text(ctx, label, x + 2, 196, energy > 0 ? PAL.amber : PAL.dim, 8);
}

function drawHUDStrip(ctx, st) {
  // Top strip: bus state badge + what the badge *means*, verbatim from the
  // gate. The blockers are shown verbatim because paraphrase is where UI
  // lies creep in.
  rect(ctx, 0, 0, W, 12, PAL.ink);
  const badge = st.state || 'OFFLINE';
  const c = badge === 'IDLE' ? PAL.green : badge === 'ACTIVE_INFER' ? PAL.red
    : badge === 'ACTIVE_USER' ? PAL.amber : PAL.dim;
  rect(ctx, 2, 2, 8, 8, c);
  text(ctx, badge, 14, 9, PAL.text, 8);
  if (st.note) text(ctx, st.note.slice(0, 46), 150, 9, PAL.dim, 8);
  if (st.dryRun) text(ctx, 'dry-run', 92, 9, PAL.amber, 8);
}

export function render(ctx, scene, events) {
  const t = performance.now() / 1000;
  const st = scene;
  drawRoom(ctx, st, t);
  for (let i = 0; i < st.pots.length; i++) {
    drawPot(ctx, st.pots[i], potBox(i), t, events);
  }
  drawHUDStrip(ctx, st);
}

export function hitTestPot(mx, my) {
  // margin of error: phones. Expand each pot box generously.
  for (let i = 0; i < 6; i++) {
    const b = potBox(i);
    if (mx >= b.x - 6 && mx <= b.x + b.w + 6 &&
        my >= b.y - 34 && my <= b.y + b.h + 14) {
      return GEN_ORDER[i];
    }
  }
  return null;
}
