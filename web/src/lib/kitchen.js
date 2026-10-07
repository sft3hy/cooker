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
    // stars twinkle between the drops — a night with stars is still a night
    for (let i = 0; i < 9; i++) {
      if ((t * 2 + i) % 3 < 1.4) {
        px(ctx, 32 + ((i * 23 + 7) % 104), 27 + ((i * 11) % 14), PAL.cream);
      }
    }
    // one shooting star every 11 seconds, because arcades deserve luck too
    const p = t % 11;
    if (p < 0.9) {
      const sx = 32 + p * 118;
      const sy = 28 + p * 9;
      px(ctx, sx, sy, PAL.emberHot);
      px(ctx, sx - 2, sy - 1, PAL.cream);
      px(ctx, sx - 4, sy - 2, '#8a7f9a');
    }
  }

  // the diner sign: OPEN when the kitchen may light, CLOSED when it may not.
  // Swings slightly. The gate's yes/no, hanging where a customer can see it.
  const open = st.ready && !busy;
  const swing = Math.round(Math.sin(t * 1.4) * 1.4);
  px(ctx, 150, 88, PAL.metalDark);
  px(ctx, 150, 89, PAL.metalDark);
  const sx0 = 136 + swing;
  rect(ctx, sx0, 90, 30, 12, PAL.ink);
  rect(ctx, sx0, 90, 30, 1, PAL.metalDark);
  const signOn = open || (t % 1) < 0.55;   // CLOSED blinks, like real neon
  text(ctx, open ? 'OPEN' : 'CLOSED', sx0 + 3, 99,
       signOn ? (open ? PAL.green : PAL.red) : '#5a2430', 8);

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

  // counter
  rect(ctx, 0, 162, W, 8, PAL.counterTop);
  rect(ctx, 0, 170, W, 2, PAL.counterEdge);
  rect(ctx, 0, 172, W, 74, PAL.counter);
  for (let x = 8; x < W; x += 48) rect(ctx, x, 178, 1, 64, PAL.counterEdge);
  rect(ctx, 0, 246, W, 24, PAL.bgShade);

  // The wall banner: one line, the honest answer to "what is the kitchen
  // doing right now", verbatim from the gate. A kitchen that never explains
  // itself is just a screensaver.
  const whyColor = st.state === 'ACTIVE_INFER' ? PAL.red
    : st.state === 'ACTIVE_USER' ? PAL.amber
    : st.state === 'IDLE' ? PAL.green : PAL.dim;
  rect(ctx, 160, 74, 316, 14, PAL.ink);
  text(ctx, (st.banner || st.state || 'offline').slice(0, 38), 164, 84, whyColor, 8);

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

  // stage word above the pot: `plan`, `extract`, `synth`… so a glance reads
  // *what* is cooking, not only that something is
  if (pot.running) {
    const kind = String(pot.running.kind || '').slice(0, 8);
    rect(ctx, x - 4, y - 40, 52, 11, PAL.ink);
    text(ctx, kind, x - 1, y - 32, PAL.steam, 8);
  }

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
  // lies creep in. The score on the right is arcade-styled but honest: it
  // is tokens actually cooked today, zero-padded like a high score, because
  // tokens are the only currency this kitchen has.
  rect(ctx, 0, 0, W, 12, PAL.ink);
  const badge = st.state || 'OFFLINE';
  const c = badge === 'IDLE' ? PAL.green : badge === 'ACTIVE_INFER' ? PAL.red
    : badge === 'ACTIVE_USER' ? PAL.amber : PAL.dim;
  rect(ctx, 2, 2, 8, 8, c);
  text(ctx, badge, 14, 9, PAL.text, 8);
  // ON AIR: blinks while the daemon actually holds a stage
  if (st.onAir) {
    const lit = (performance.now() / 500) % 2 < 1;
    rect(ctx, 92, 2, 4, 8, lit ? PAL.red : '#5a2430');
    text(ctx, 'AIR', 98, 9, lit ? PAL.red : '#5a2430', 8);
  }
  if (st.dryRun) text(ctx, 'dry-run', 128, 9, PAL.amber, 8);
  if (st.note) text(ctx, st.note.slice(0, 24), 150, 9, PAL.dim, 8);
  const score = String(Math.max(0, Math.round(st.tokensToday || 0)));
  const pad = '0'.repeat(Math.max(0, 7 - score.length));
  text(ctx, `SCORE ${pad}${score}`, 330, 9, PAL.amber, 8);
}

function drawServeCelebration(ctx, st, t) {
  // PLATE SERVED — confetti + ribbon, ~2.8s, fired by real events
  // (runner.published / chain.complete / digest.written). The confetti is
  // deterministic-per-frame, not random: same instant, same picture, and a
  // celebration that never allocates is a celebration that never janks.
  const colors = [PAL.emberHot, PAL.cream, PAL.flame, PAL.steam, PAL.amber];
  for (let i = 0; i < 26; i++) {
    const cx = (i * 37 + Math.round(t * 90)) % W;
    const cy = (i * 53 + Math.round(t * 150)) % 150;
    ctx.fillStyle = colors[i % colors.length];
    ctx.fillRect(cx, cy, 2, 2);
  }
  rect(ctx, 132, 118, 216, 18, PAL.ink);
  rect(ctx, 132, 118, 216, 1, PAL.amber);
  rect(ctx, 132, 135, 216, 1, PAL.amber);
  const word = st.flashKind === 'digest' ? 'DIGEST PRINTED!' : 'PLATE SERVED!';
  text(ctx, word, 150 + ((Math.round(t * 6) % 2) ? 1 : 0), 131, PAL.emberHot, 8);
}

export function render(ctx, scene, events) {
  const t = performance.now() / 1000;
  const st = scene;
  drawRoom(ctx, st, t);
  for (let i = 0; i < st.pots.length; i++) {
    drawPot(ctx, st.pots[i], potBox(i), t, events);
  }
  if (st.flashUntil && Date.now() < st.flashUntil) {
    drawServeCelebration(ctx, st, t);
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
