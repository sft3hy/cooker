<script>
  import { onMount, onDestroy } from 'svelte';
  import { render, hitTestPot, W, H } from './lib/kitchen.js';
  import { sfxFor, bubble, isMuted, setMuted, unlock } from './lib/sfx.js';

  let canvas, wrap;
  let scale = $state(2);
  let state = $state(null);
  let log = $state([]);            // recent events for the ticker + drawer
  let drawer = $state(null);       // generator name | null
  let artifact = $state(null);     // {id,title,text} | null
  let muted = $state(isMuted());
  let sseNote = $state('connecting');
  let lastEventId = 0;

  let es = null;
  let pollTimer = null;
  let bubbleTimer = null;
  let raf = 0;

  const HEAT = { filling: 1, simmer: 1, boiling: 1 };

  async function refreshState() {
    try {
      const r = await fetch('./api/state');
      if (!r.ok) throw new Error(`state ${r.status}`);
      state = await r.json();
      sseNote = state.gate?.stale ? 'detached' : 'live';
    } catch (e) {
      sseNote = 'offline';
    }
  }

  function onEvent(ev) {
    let d = {};
    try { d = JSON.parse(ev.data); } catch { return; }
    lastEventId = d.id || lastEventId;
    log = [...log.slice(-249), d];
    if (d.type === 'runner.stage' || d.type === 'task.queued') refreshState();
    if (d.type === 'runner.published' || d.type === 'chain.complete'
        || d.type === 'runner.rejected' || d.type === 'digest.written'
        || d.type === 'llm.abort' || d.type === 'runner.cancelled') refreshState();
    sfxFor(d.type);
  }

  function connect() {
    es = new EventSource(`./api/events${lastEventId ? `?after=${lastEventId}` : ''}`);
    es.onopen = () => { sseNote = 'live'; };
    es.onerror = () => {
      sseNote = 'reconnecting';
      // EventSource auto-retries with Last-Event-ID; nothing to rebuild here.
    };
    const types = ['bus.state', 'task.queued', 'task.paused', 'task.recovered',
      'runner.stage', 'runner.published', 'runner.rejected', 'runner.cancelled',
      'runner.failed', 'runner.empty', 'chain.complete', 'chain.rejected',
      'chain.stalled', 'chain.stub', 'digest.written', 'publish.blocked',
      'publish.duplicate', 'llm.abort', 'daemon.start', 'daemon.stop',
      'daemon.seeded', 'dry.would_seed', 'generator.parked', 'generator.unparked',
      'safety.redaction', 'safety.withheld', 'search.done', 'fetch.failed'];
    for (const t of types) es.addEventListener(t, onEvent);
  }

  function computeScale() {
    if (!wrap) return;
    const availW = wrap.clientWidth - 8;
    const availH = Math.max(140, window.innerHeight * 0.46);
    const s = Math.max(1, Math.min(4, Math.floor(Math.min(availW / W, availH / H))));
    scale = s;
  }

  function potScene() {
    // The canvas scene: map the API payload to what the renderer draws.
    if (!state) return { pots: [], state: 'OFFLINE', busy: false };
    const g = state.gate || {};
    const busy = g.stale ? false : g.state !== 'IDLE';
    let banner;
    if (g.stale) {
      banner = 'daemon detached — last known state';
    } else if (g.state === 'ACTIVE_INFER') {
      banner = 'paused — someone is generating';
    } else if (g.state === 'ACTIVE_USER') {
      banner = 'waiting — activity on the box';
    } else if ((state.workers?.slots || []).length) {
      banner = `cooking — ${state.workers.slots.length} stage(s) on`;
    } else if (g.ready && !g.ramped) {
      banner = `one pot — ${(g.ramp_note || 'ramping').slice(0, 24)}`;
    } else if (g.ready) {
      banner = 'ready — filling the queue';
    } else {
      banner = `quiet ${(g.quiet_s ?? 0).toFixed(0)}s of ${(g.ready_in_s || 4).toFixed(0)}`;
    }
    return {
      pots: state.pots,
      state: g.stale ? 'OFFLINE?' : g.state,
      busy: busy || g.state === 'ACTIVE_INFER',
      banner,
      note: (g.reason || '').slice(0, 46),
      digestWritten: state.digest?.written,
      platedToday: state.today?.published ?? 0,
      dryRun: state.workers?.dry_run,
    };
  }

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
      artifact = null;
    } else {
      drawer = null;
    }
  }

  function toggleMute() {
    muted = !muted;
    setMuted(muted);
    if (!muted) { unlock(); sfxFor('digest.written'); }
  }

  async function openArtifact(id) {
    const r = await fetch(`./api/artifacts/${encodeURIComponent(id)}`);
    const text = await r.text();
    artifact = { id, status: r.headers.get('X-Artifact-Status') || '?', text };
  }

  onMount(async () => {
    computeScale();
    window.addEventListener('resize', computeScale);
    // fonts change metrics and the fit is arithmetic on them; redraw is every
    // frame anyway, but re-fitting after the pixel font lands keeps the
    // integer scale honest.
    if (document.fonts?.ready) document.fonts.ready.then(computeScale);
    await refreshState();
    connect();
    pollTimer = setInterval(refreshState, 4000);
    bubbleTimer = setInterval(() => {
      if (state?.pots?.some(p => HEAT[p.state])) bubble();
    }, 700);
    raf = requestAnimationFrame(frame);
  });

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(bubbleTimer);
    cancelAnimationFrame(raf);
    es?.close();
    window.removeEventListener('resize', computeScale);
  });

  // drawer helpers
  function drawerData() {
    if (!drawer || !state) return null;
    const pot = state.pots.find(p => p.generator === drawer);
    const genEvents = log.filter(e => e.generator === drawer).slice(-14).reverse();
    const arts = (state.artifacts || []).filter(a => a.generator === drawer);
    return { pot, genEvents, arts };
  }

  function fmtAge(s) {
    if (s == null) return '';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  let gateBar = $derived.by(() => {
    const g = state?.gate || {};
    if (!g.ready && g.ready_in_s > 0) return { label: `ready in ${Math.ceil(g.ready_in_s)}s`, pct: 0 };
    if (g.ramp_note) return { label: g.ramp_note.slice(0, 40), pct: 0.5 };
    if (g.quiet_s != null) return { label: `quiet ${Math.round(g.quiet_s)}s`, pct: 1 };
    return { label: g.stale ? 'last known' : '', pct: 0 };
  });

  let running = $derived((state?.workers?.slots || []).length || (state?.running || []).length);
</script>

<main>
  <header>
    <h1>cooker</h1>
    <span class="conn" class:live={sseNote === 'live'}>{sseNote}</span>
    <button class="mute" onclick={toggleMute} aria-label="toggle sound">
      {muted ? '♪ off' : '♪ on'}
    </button>
  </header>

  <section class="scene" bind:this={wrap}>
    <canvas
      bind:this={canvas}
      width={W}
      height={H}
      style="width:{W * scale}px; height:{H * scale}px;"
      onclick={onClick}
      aria-label="pixel kitchen: six burners, one per generator — click a pot to inspect it"
    ></canvas>
  </section>

  <section class="hud">
    <div class="badge">
      {#if state?.gate}
        <b>{state.gate.state}</b>
        <span class="why">{(state.gate.reason || '').slice(0, 60)}</span>
        <span class="bar"><i style="width:{gateBar.pct * 100}%"></i></span>
        <span class="gate">{gateBar.label}</span>
      {:else}
        <span class="why">no state</span>
      {/if}
    </div>
    <dl>
      <div><dt>stages</dt><dd>{state?.today?.stages ?? '—'}</dd></div>
      <div><dt>gpu</dt><dd>{state?.today?.gpu_seconds ?? '—'}s</dd></div>
      <div><dt>tokens</dt><dd>{((state?.today?.input_tokens ?? 0) + (state?.today?.output_tokens ?? 0)).toLocaleString()}</dd></div>
      <div><dt>p95 ttft</dt><dd>{state?.workers?.p95_ttft_s ?? '—'}</dd></div>
      <div><dt>preempt</dt><dd>{state?.today?.preemptions ?? '—'}</dd></div>
      <div><dt>plated</dt><dd>{state?.today?.published ?? 0}</dd></div>
      <div><dt>running</dt><dd>{running}</dd></div>
    </dl>
  </section>

  {#if drawer}
    {@const d = drawerData()}
    <section class="drawer">
      <div class="drawer-head">
        <h2>{drawer}</h2>
        <button onclick={() => { drawer = null; artifact = null; }}>close</button>
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
        {#if artifact}
          <pre class="md">{artifact.text}</pre>
        {:else}
          {#if d.arts.length}
            <h3>plates</h3>
            <ul class="arts">
              {#each d.arts as a}
                <li>
                  <button onclick={() => openArtifact(a.id)}>{a.title}</button>
                  <span class="score">{a.score ?? '—'} <em>{a.status.toLowerCase()}</em> {fmtAge(a.age_s)}</span>
                </li>
              {/each}
            </ul>
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
      {/if}
    </section>
  {/if}

  <section class="ticker">
    {#each log.slice(-8).reverse() as e}
      <p class={e.severity}><code>{e.type}</code> {(e.message || '').slice(0, 110)}</p>
    {/each}
  </section>

  <footer>
    {#if state?.topics?.count}
      <span>{state.topics.count} topics, next: {(state.topics.list?.[0] || '').slice(0, 50)}</span>
    {/if}
    <span class="disk">{state ? (state.disk.bytes / 1024 / 1024).toFixed(1) : '—'} MB</span>
  </footer>
</main>

<style>
  :global(:root) {
    --bg: #1b1524;
    --panel: #241c2e;
    --ink: #f7f3e8;
    --dim: #8a7f9a;
    --amber: #ffb347;
    --ember: #ff9f43;
    --cream: #f2e7cf;
    --cyan: #9fe8ea;
    --red: #e05252;
    --green: #6fcf7c;
  }
  /* The preload in index.html is nothing without this declaration — the font
     silently fell back to 8px monospace, which is how the kitchen came to
     look "wonky": every canvas label was drawing in a font that never
     loaded. (The @font-face itself lives in index.html, not here: Vite
     resolves url() in bundled CSS relative to /assets/, and public/ is not
     under /assets/. A path that works in dev and 404s in prod is the worst
     kind of works-on-my-machine.) */
  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 13px/1.5 ui-monospace, "SF Mono", Menlo, monospace;
  }
  main {
    max-width: 1080px;
    margin: 0 auto;
    padding: 8px 12px 40px;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 4px 2px;
  }
  h1 {
    font-family: "Press Start 2P", monospace;
    font-size: 14px;
    margin: 0;
    color: var(--cream);
  }
  .conn { color: var(--dim); font-size: 11px; }
  .conn.live { color: var(--green); }
  .mute {
    margin-left: auto;
    background: var(--panel);
    color: var(--cream);
    border: 2px solid var(--dim);
    font-family: "Press Start 2P", monospace;
    font-size: 8px;
    padding: 6px 8px;
    cursor: pointer;
  }
  .scene { text-align: center; margin: 6px 0 10px; }
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
  dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
    gap: 4px;
    margin: 6px 0 0;
  }
  dl > div {
    background: var(--panel);
    border: 2px solid #322640;
    padding: 4px 8px;
  }
  dt { font-size: 9px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; }
  dd { margin: 0; font-family: "Press Start 2P", monospace; font-size: 11px; color: var(--cream); }
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
  .log, .ticker { list-style: none; padding: 0; margin: 0; font-size: 11px; }
  .log li, .ticker p { padding: 1px 0; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .log code, .ticker code { color: var(--cyan); font-size: 10px; }
  .warn { color: var(--amber); }
  .error { color: var(--red); }
  .dim { color: #55506a; }
  .ticker {
    border-top: 2px solid #322640;
    margin-top: 14px;
    padding-top: 6px;
  }
  footer {
    display: flex;
    justify-content: space-between;
    color: var(--dim);
    font-size: 11px;
    margin-top: 10px;
  }
  @media (max-width: 520px) {
    dl { grid-template-columns: repeat(3, 1fr); }
    h1 { font-size: 12px; }
  }
</style>
