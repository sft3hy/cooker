<script>
  // The stove view: the pixel canvas, the gate badge, the drawer, the ticker.
  // Extracted from App.svelte when the kitchen grew rooms (§ tabs); the
  // renderer in kitchen.js is untouched — this file only owns its lifecycle,
  // so mounting/unmounting a tab starts and stops the raf loop honestly
  // instead of drawing into a detached canvas.
  import { onMount, onDestroy } from 'svelte';
  import { render, hitTestPot, W, H } from './kitchen.js';
  import { bubble, unlock } from './sfx.js';

  let { snap, log, serveFlash, openArtifact } = $props();

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
</style>
