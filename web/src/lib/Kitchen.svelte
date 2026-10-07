<script>
  // The stove view: the pixel canvas, the gate badge, the drawer, the ticker.
  // Extracted from App.svelte when the kitchen grew rooms (§ tabs); the
  // renderer in kitchen.js is untouched — this file only owns its lifecycle,
  // so mounting/unmounting a tab starts and stops the raf loop honestly
  // instead of drawing into a detached canvas.
  import { onMount, onDestroy } from 'svelte';
  import { render, hitTestPot, W, H } from './kitchen.js';
  import { bubble, unlock } from './sfx.js';

  let { snap, log, serveFlash, openArtifact, onOrdered } = $props();

  // The order counter. Sam writes, the kitchen takes the ticket. deep-dive
  // means "put it to opencode and let it dig" (minutes, one big burn);
  // research is the kitchen's own five-stage way. Neither buys a pass at
  // the judge — the ticket buys attention, not the verdict.
  let order = $state({ topic: '', kind: 'deep-dive', note: '', busy: false });
  let tickets = $state([]);

  async function loadTickets() {
    try {
      const r = await fetch('./api/orders');
      if (r.ok) tickets = (await r.json()).orders || [];
    } catch { /* the counter can wait */ }
  }

  async function submitOrder() {
    const topic = order.topic.trim();
    if (topic.length < 8) { order.note = 'a topic of 8+ characters, please'; return; }
    order.busy = true; order.note = '';
    try {
      const r = await fetch('./api/orders', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ topic, kind: order.kind }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        order.note = '✓ order taken — it queues like everything else';
        order.topic = '';
        onOrdered?.();
        loadTickets();
      } else {
        order.note = d.error || `the counter says no (${r.status})`;
      }
    } catch {
      order.note = 'the counter is unreachable';
    } finally {
      order.busy = false;
    }
  }

  let canvas = $state(null);
  let wrap = $state(null);
  let scale = $state(2);
  let drawer = $state(null);   // generator name | null
  let raf = 0;
  let bubbleTimer = null;

  const HEAT = { filling: 1, simmer: 1, boiling: 1 };

  function computeScale() {
    if (!wrap) return;
    const availW = wrap.clientWidth - 8;
    const availH = Math.max(140, window.innerHeight * 0.46);
    const s = Math.max(1, Math.min(4, Math.floor(Math.min(availW / W, availH / H))));
    scale = s;
  }

  function potScene() {
    // The canvas scene: map the API payload to what the renderer draws.
    if (!snap) return { pots: [], state: 'OFFLINE', busy: false };
    const g = snap.gate || {};
    const busy = g.stale ? false : g.state !== 'IDLE';
    let banner;
    if (g.stale) {
      banner = 'daemon detached — last known state';
    } else if (g.state === 'ACTIVE_INFER') {
      banner = 'paused — someone is generating';
    } else if (g.state === 'ACTIVE_USER') {
      // since §20b only blindness lands here — typing does not. say so.
      banner = (g.reason || '').includes('blind')
        ? 'blind — cannot see the GPU'
        : 'waiting — presence blocks (legacy mode)';
    } else if ((snap.workers?.slots || []).length) {
      banner = `cooking — ${snap.workers.slots.length} stage(s) on`;
    } else if (g.ready && !g.ramped) {
      banner = `one pot — ${(g.ramp_note || 'ramping').slice(0, 24)}`;
    } else if (g.ready) {
      banner = 'ready — filling the queue';
    } else {
      banner = `quiet ${(g.quiet_s ?? 0).toFixed(0)}s of ${(g.ready_in_s || 4).toFixed(0)}`;
    }
    return {
      pots: snap.pots,
      state: g.stale ? 'OFFLINE?' : g.state,
      busy: busy || g.state === 'ACTIVE_INFER',
      ready: !!(g.ready && !busy && !g.stale),
      banner,
      note: (g.reason || '').slice(0, 46),
      digestWritten: snap.digest?.written,
      platedToday: snap.today?.published ?? 0,
      dryRun: snap.workers?.dry_run,
      onAir: !g.stale && !!((snap.workers?.slots || []).length || running),
      tokensToday: (snap.today?.input_tokens ?? 0) + (snap.today?.output_tokens ?? 0),
      flashUntil: serveFlash.until,
      flashKind: serveFlash.kind,
    };
  }

  let running = $derived((snap?.workers?.slots || []).length || (snap?.running || []).length);

  let gateBar = $derived.by(() => {
    const g = snap?.gate || {};
    if (!g.ready && g.ready_in_s > 0) return { label: `ready in ${Math.ceil(g.ready_in_s)}s`, pct: 0 };
    if (g.ramp_note) return { label: g.ramp_note.slice(0, 40), pct: 0.5 };
    if (g.quiet_s != null) return { label: `quiet ${Math.round(g.quiet_s)}s`, pct: 1 };
    return { label: g.stale ? 'last known' : '', pct: 0 };
  });

  function frame() {
    if (canvas) {
      const ctx = canvas.getContext('2d');
      ctx.imageSmoothingEnabled = false;
      render(ctx, potScene(), log);
    }
    raf = requestAnimationFrame(frame);
  }

  function onClick(e) {
    unlock();
    const rect = canvas.getBoundingClientRect();
    // logical coords from the *rendered* rect, not the assumed scale: if a
    // font load or zoom nudged the layout between frames, the rect is still
    // the truth and a stale `scale` is not.
    const mx = (e.clientX - rect.left) * (W / rect.width);
    const my = (e.clientY - rect.top) * (H / rect.height);
    const gen = hitTestPot(mx, my);
    if (gen) {
      drawer = gen;
    }
  }

  // drawer helpers
  function drawerData() {
    if (!drawer || !snap) return null;
    const pot = snap.pots.find(p => p.generator === drawer);
    const genEvents = log.filter(e => e.generator === drawer).slice(-14).reverse();
    const arts = (snap.artifacts || []).filter(a => a.generator === drawer);
    return { pot, genEvents, arts };
  }

  function fmtAge(s) {
    if (s == null) return '';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  onMount(() => {
    loadTickets();
    computeScale();
    window.addEventListener('resize', computeScale);
    // fonts change metrics and the fit is arithmetic on them; redraw is every
    // frame anyway, but re-fitting after the pixel font lands keeps the
    // integer scale honest.
    if (document.fonts?.ready) document.fonts.ready.then(computeScale);
    raf = requestAnimationFrame(frame);
    bubbleTimer = setInterval(() => {
      if (snap?.pots?.some(p => HEAT[p.state])) bubble();
    }, 700);
  });

  onDestroy(() => {
    cancelAnimationFrame(raf);
    clearInterval(bubbleTimer);
    window.removeEventListener('resize', computeScale);
  });
</script>

<section class="scene" bind:this={wrap}>
  <div class="tv">
    <canvas
      bind:this={canvas}
      width={W}
      height={H}
      style="width:{W * scale}px; height:{H * scale}px;"
      onclick={onClick}
      aria-label="pixel kitchen: burners, one per generator — click a pot to inspect it"
    ></canvas>
    <!-- CRT scanlines + vignette: pure decoration, pointer-events none,
         and it lives on TOP of the canvas so it never touches the logical
         pixels — the integers underneath stay integers. -->
    <div class="scan" aria-hidden="true"></div>
  </div>
</section>

<section class="hud">
  <div class="badge">
    {#if snap?.gate}
      <b>{snap.gate.state}</b>
      <span class="why">{(snap.gate.reason || '').slice(0, 60)}</span>
      <span class="bar"><i style="width:{gateBar.pct * 100}%"></i></span>
      <span class="gate">{gateBar.label}</span>
    {:else}
      <span class="why">no state</span>
    {/if}
  </div>
</section>

<section class="counter">
  <div class="rail">
    <span class="sign" class:closed={!snap?.gate?.ready && !(snap?.workers?.slots || []).length}
          class:cooking={(snap?.workers?.slots || []).length > 0}>
      {(snap?.workers?.slots || []).length ? 'COOKING' : (snap?.gate?.ready ? 'OPEN' : 'CLOSED')}
    </span>
    <h2>order at the counter</h2>
  </div>
  <div class="ticket">
    <textarea class="paper" rows="7" placeholder="what should the kitchen look into? write as long as you like — context is welcome (min 8 chars)"
              bind:value={order.topic}></textarea>
    <div class="ticket-foot">
      <div class="kinds">
        <button class="k" class:on={order.kind === 'deep-dive'} onclick={() => order.kind = 'deep-dive'}>DEEP-DIVE</button>
        <button class="k" class:on={order.kind === 'research'} onclick={() => order.kind = 'research'}>RESEARCH</button>
      </div>
      <button class="send" onclick={submitOrder} disabled={order.busy}>{order.busy ? 'writing…' : 'SEND ORDER'}</button>
    </div>
    <p class="mean">deep-dive = hand it to opencode and let it dig (minutes). research = the five-stage way, tonight's quieter kitchen. both face the same judge — ordering never buys a pass.</p>
    {#if order.note}<p class="note" class:ok={order.note.startsWith('✓')}>{order.note}</p>{/if}
  </div>
  {#if tickets.length}
    <ul class="tickets">
      {#each tickets as tk (tk.id)}
        <li>
          <span class="tk-kind">{tk.kind}</span>
          <span class="tk-topic">{tk.topic}</span>
          <span class="tk-status s-{tk.status.toLowerCase()}">{tk.status.toLowerCase()}{#if tk.score != null} · {tk.score}{/if}</span>
        </li>
      {/each}
    </ul>
  {/if}
</section>

{#if drawer}
  {@const d = drawerData()}
  <section class="drawer">
    <div class="drawer-head">
      <h2>{drawer}</h2>
      <button onclick={() => { drawer = null; }}>close</button>
    </div>
    {#if d?.pot}
      <p class="potstate">
        {d.pot.state}
        {#if d.pot.running}
          · {d.pot.running.kind} · {d.pot.running.elapsed_s}s
        {/if}
        {#if d.pot.queued}· {d.pot.queued} queued{/if}
        {#if d.pot.backoff_s}· backoff {Math.ceil(d.pot.backoff_s)}s{/if}
      </p>
      {#if d.arts.length}
        <h3>recent plates from this pot</h3>
        <ul class="arts">
          {#each d.arts.slice(0, 5) as a}
            <li>
              <button onclick={() => openArtifact(a.id)}>{a.title}</button>
              <span class="score">{a.score ?? '—'} <em>{a.status.toLowerCase()}</em> {fmtAge(a.age_s)}</span>
            </li>
          {/each}
        </ul>
        <p class="hint">the full pantry lives under PANTRY (key 2)</p>
      {/if}
      <h3>log</h3>
      <ul class="log">
        {#each d.genEvents as e}
          <li class={e.severity}><code>{e.type}</code> {e.message || ''}</li>
        {:else}
          <li class="dim">nothing yet</li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<section class="ticker">
  {#each log.slice(-8).reverse() as e}
    <p class={e.severity}><code>{e.type}</code> {(e.message || '').slice(0, 110)}</p>
  {/each}
</section>

<style>
  .scene { text-align: center; margin: 6px 0 10px; }
  .tv { position: relative; display: inline-block; line-height: 0; }
  .scan {
    position: absolute;
    inset: 0;
    pointer-events: none;
    background:
      radial-gradient(ellipse at center, transparent 62%, rgba(8, 4, 14, 0.38) 100%),
      repeating-linear-gradient(0deg, rgba(0, 0, 0, 0.20) 0 1px, transparent 1px 3px);
    mix-blend-mode: multiply;
  }
  canvas {
    image-rendering: pixelated;
    image-rendering: crisp-edges;
    border: 4px solid #322640;
    background: #241c2e;
    cursor: pointer;
    /* no max-width here on purpose: `width:{W*scale}px` with an integer
       scale already fits — computeScale floors to the container. A max-width
       that re-shrinks the canvas turns the integer scale back into a
       fractional one, and fractional is exactly the smear we declared war
       on. */
  }
  .hud { margin: 4px 0 10px; }
  .badge {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--panel);
    border: 2px solid #322640;
    padding: 6px 10px;
    flex-wrap: wrap;
  }
  .badge b { font-family: "Press Start 2P", monospace; font-size: 10px; color: var(--amber); }
  .badge .why { color: var(--dim); font-size: 11px; }
  .badge .gate { color: var(--cyan); font-size: 11px; }
  .bar { flex: 1 1 80px; height: 6px; background: #16101e; min-width: 60px; }
  .bar i { display: block; height: 100%; background: var(--green); }
  .drawer {
    background: var(--panel);
    border: 2px solid var(--amber);
    padding: 8px 12px;
    margin: 8px 0;
  }
  .drawer-head { display: flex; justify-content: space-between; align-items: center; }
  h2 { font-family: "Press Start 2P", monospace; font-size: 11px; color: var(--amber); margin: 4px 0; }
  h3 { font-size: 10px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; margin: 10px 0 4px; }
  .potstate { color: var(--cyan); font-size: 12px; }
  .drawer button {
    background: none; border: 1px solid var(--dim); color: var(--cream);
    font: inherit; cursor: pointer; padding: 2px 6px;
  }
  .arts { list-style: none; padding: 0; margin: 0; }
  .arts li { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; }
  .arts button { border: none; border-bottom: 1px dotted var(--amber); text-align: left; flex: 1; padding: 0; }
  .score { color: var(--dim); font-size: 11px; white-space: nowrap; }
  .hint { color: #55506a; font-size: 10px; margin: 6px 0 0; }
  .log { list-style: none; padding: 0; margin: 0; font-size: 11px; }
  .log li { padding: 1px 0; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .log code { color: var(--cyan); font-size: 10px; }
  .warn { color: var(--amber); }
  .error { color: var(--red); }
  .dim { color: #55506a; }
  .ticker {
    border-top: 2px solid #322640;
    margin-top: 14px;
    padding-top: 6px;
    font-size: 11px;
  }
  .ticker p { padding: 1px 0; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin: 0; }
  .ticker code { color: var(--cyan); font-size: 10px; }

  .counter { margin: 14px 0 4px; }
  .rail { display: flex; align-items: center; gap: 12px; }
  .counter h2 { font-family: "Press Start 2P", monospace; font-size: 10px; color: var(--dim); margin: 0; }
  .sign {
    font-family: "Press Start 2P", monospace; font-size: 11px; padding: 6px 10px;
    color: var(--green); border: 2px solid var(--green);
    text-shadow: 0 0 8px rgba(111, 207, 124, 0.9), 0 0 22px rgba(111, 207, 124, 0.45);
    animation: hum 2.4s steps(2, jump-none) infinite;
  }
  .sign.closed { color: var(--red); border-color: var(--red);
    text-shadow: 0 0 8px rgba(224, 82, 82, 0.9), 0 0 22px rgba(224, 82, 82, 0.4); animation: none; }
  .sign.cooking { color: var(--amber); border-color: var(--amber);
    text-shadow: 0 0 8px rgba(255, 179, 71, 0.95), 0 0 24px rgba(255, 159, 67, 0.5); animation: none; }
  @keyframes hum { 50% { opacity: 0.82; } }
  .ticket {
    margin-top: 8px; background: var(--paper, #f2e7cf);
    border: 2px solid #cbb98f; padding: 10px 12px;
    box-shadow: 0 3px 0 #16101e; transform: rotate(-0.35deg);
  }
  .paper {
    width: 100%; box-sizing: border-box; background: transparent; resize: vertical;
    border: none; border-bottom: 1px dashed #b39b6a; outline: none;
    font: 13px/1.7 ui-monospace, "SF Mono", Menlo, monospace; color: #3a2f1d;
  }
  .paper::placeholder { color: #9c8a63; }
  .ticket-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; gap: 8px; flex-wrap: wrap; }
  .kinds { display: flex; gap: 6px; }
  .k, .send {
    font-family: "Press Start 2P", monospace; font-size: 8px; cursor: pointer;
    background: transparent; border: 2px solid #8a7654; color: #5c4a2e; padding: 7px 9px;
  }
  .k.on { background: #5c4a2e; color: #f2e7cf; border-color: #5c4a2e; }
  .send { border-color: #7a4f1d; color: #7a4f1d; }
  .send:hover { background: var(--amber); color: #241c2e; }
  .mean { color: #8a7654; font-size: 10px; margin: 8px 0 0; line-height: 1.5; }
  .note { color: #7a4f1d; font-size: 11px; margin: 6px 0 0; }
  .note.ok { color: #2f7d3c; }
  .tickets { list-style: none; padding: 0; margin: 10px 0 0; }
  .tickets li {
    display: flex; gap: 8px; align-items: baseline; font-size: 11px;
    padding: 3px 8px; border-bottom: 1px dotted #322640; color: var(--dim);
  }
  .tk-kind { font-family: "Press Start 2P", monospace; font-size: 7px; color: var(--cyan); }
  .tk-topic { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .s-running { color: var(--amber); }
  .s-succeeded, .s-published { color: var(--green); }
  .s-failed, .s-rejected { color: var(--red); }

</style>
