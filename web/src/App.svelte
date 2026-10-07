<script>
  import { onMount, onDestroy } from 'svelte';
  import { sfxFor, isMuted, setMuted, unlock } from './lib/sfx.js';
  import Kitchen from './lib/Kitchen.svelte';
  import Pantry from './lib/Pantry.svelte';
  import Stats from './lib/Stats.svelte';
  import Guide from './lib/Guide.svelte';
  import { renderMarkdown } from './lib/md.js';

  let tab = $state(localStorage.getItem('cooker.tab') || 'kitchen');
  let state = $state(null);
  let log = $state([]);            // recent events for the ticker + drawer
  let stats = $state(null);       // the cook's books, for the STATS tab
  let refreshKey = $state(0);     // bumps make Pantry re-shelve on events
  let muted = $state(isMuted());
  let sseNote = $state('connecting');
  let lastEventId = 0;

  let es = null;
  let pollTimer = null;
  let statsTimer = null;
  // serveFlash: real events (published / digest) light the celebration for
  // 2.8s. It is driven off the event stream, never off a timer of its own —
  // a kitchen that congratulates itself on a schedule is a lie with confetti.
  let serveFlash = $state({ until: 0, kind: '' });
  // the shared reader: one overlay, opened from the kitchen drawer or the
  // pantry jars; text is fetched fresh every time (a stale read of a file
  // the judge already scored would be the UI equivalent of a warm reheated
  // plate pretending it was just cooked).
  let artifact = $state(null);     // {id,title,status,score,text} | null

  const TABS = [
    ['kitchen', 'KITCHEN'],
    ['pantry', 'PANTRY'],
    ['stats', 'STATS'],
    ['guide', 'GUIDE'],
  ];

  function setTab(t) {
    tab = t;
    localStorage.setItem('cooker.tab', t);
    // the stats $effect re-books whenever the tab opens — no double fetch
  }

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

  function onOrdered() {
    refreshKey++;
    refreshState();
    refreshStats();
  }

  async function refreshStats() {
    try {
      const r = await fetch('./api/stats');
      if (!r.ok) throw new Error(`stats ${r.status}`);
      stats = await r.json();
    } catch { /* keep the last honest books */ }
  }

  async function openArtifact(ref, meta) {
    try {
      const r = await fetch(`./api/artifacts/${encodeURIComponent(ref)}`);
      const text = await r.text();
      artifact = {
        id: ref,
        title: meta?.title || ref,
        status: r.headers.get('X-Artifact-Status') || (r.ok ? '?' : 'gone'),
        score: meta?.score ?? null,
        text,
      };
    } catch {
      artifact = { id: ref, title: ref, status: '?', score: null, text: '(cannot be read)' };
    }
  }

  async function openDigest() {
    try {
      const r = await fetch('./api/digest');
      const text = await r.text();
      artifact = {
        id: 'digest', title: r.ok ? "today's menu" : 'the menu',
        status: r.ok ? 'DIGEST' : 'not printed', score: null, text,
      };
    } catch {
      artifact = { id: 'digest', title: "today's menu", status: '?', score: null, text: '(cannot be read)' };
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
        || d.type === 'llm.abort' || d.type === 'runner.cancelled') {
      refreshState();
      refreshStats();
      refreshKey++;
    }
    if (d.type === 'runner.published' || d.type === 'chain.complete') {
      serveFlash = { until: Date.now() + 2800, kind: 'plate' };
    } else if (d.type === 'digest.written') {
      serveFlash = { until: Date.now() + 2800, kind: 'digest' };
    }
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

  function toggleMute() {
    muted = !muted;
    setMuted(muted);
    if (!muted) { unlock(); sfxFor('digest.written'); }
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Escape') { artifact = null; return; }
    const hit = TABS.find(([key], i) => String(i + 1) === e.key);
    if (hit) setTab(hit[0]);
  }

  $effect(() => {
    if (tab !== 'stats') return;
    refreshStats();
    statsTimer = setInterval(refreshStats, 15000);
    return () => clearInterval(statsTimer);
  });

  onMount(async () => {
    window.addEventListener('keydown', onKey);
    await refreshState();
    connect();
    pollTimer = setInterval(refreshState, 4000);
  });

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(statsTimer);
    es?.close();
    window.removeEventListener('keydown', onKey);
  });
</script>

<main>
  <header>
    <h1><span class="star" aria-hidden="true">★</span> cooker</h1>
    <span class="conn" class:live={sseNote === 'live'}>{sseNote}</span>
    <button class="mute" onclick={toggleMute} aria-label="toggle sound">
      {muted ? '♪ off' : '♪ on'}
    </button>
  </header>

  <nav class="tabs" aria-label="rooms">
    {#each TABS as [key, label] (key)}
      <button class="tab" class:on={tab === key} onclick={() => setTab(key)}>{label}</button>
    {/each}
  </nav>

  {#if tab === 'kitchen'}
    <Kitchen snap={state} {log} {serveFlash} {openArtifact} onOrdered={onOrdered} />
  {:else if tab === 'pantry'}
    <Pantry refreshKey={refreshKey} {openArtifact} {openDigest} />
  {:else if tab === 'stats'}
    <Stats snap={state} {stats} />
  {:else}
    <Guide />
  {/if}

  <footer>
    {#if state?.topics?.count}
      <span>{state.topics.count} topics, next: {(state.topics.list?.[0] || '').slice(0, 50)}</span>
    {/if}
    <span class="disk">{state ? (state.disk.bytes / 1024 / 1024).toFixed(1) : '—'} MB</span>
  </footer>
</main>

{#if artifact}
  <div class="overlay" role="dialog" aria-modal="true" aria-label="the dish">
    <section class="plate">
      <div class="plate-head">
        <h2>{artifact.title}</h2>
        <span class="stamp" class:gold={artifact.status === 'PUBLISHED'}>
          {artifact.status}{#if artifact.score != null} · {artifact.score}{/if}
        </span>
        <div class="plate-actions">
          <button onclick={() => {
            navigator.clipboard?.writeText(artifact.text);
          }}>copy</button>
          <button onclick={() => { artifact = null; }}>close (esc)</button>
        </div>
      </div>
      <article class="md">{@html renderMarkdown(artifact.text)}</article>
    </section>
  </div>
{/if}

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
  /* The rendered plate: a cream page on the dark table, serif-free but
     calm, amber section heads like the brass rail. Content is sanitized
     by lib/md.js before it gets here — structure only, no raw tags. */
  :global(.md) {
    font: 13px/1.75 ui-monospace, "SF Mono", Menlo, monospace;
    color: #2c2433;
    background: var(--paper, #f2e7cf);
    padding: 18px 20px;
    border: 2px solid #cbb98f;
    box-shadow: inset 0 0 0 1px #fff8e6, 0 2px 0 #16101e;
    word-break: break-word;
  }
  :global(.md h3), :global(.md h4), :global(.md h5), :global(.md h6) {
    font-family: "Press Start 2P", monospace;
    color: #7a4f1d;
    font-size: 10px;
    letter-spacing: 0.5px;
    margin: 18px 0 8px;
    border-bottom: 1px dashed #cbb98f;
    padding-bottom: 4px;
  }
  :global(.md p) { margin: 8px 0; }
  :global(.md ul), :global(.md ol) { margin: 8px 0; padding-left: 20px; }
  :global(.md li) { margin: 3px 0; }
  :global(.md code) {
    background: #e4d6b4; border: 1px solid #cbb98f;
    padding: 0 3px; font-size: 12px; color: #5a3d12;
  }
  :global(.md pre.code) {
    background: #241c2e; color: var(--cyan);
    border: 2px solid #cbb98f; padding: 10px 12px;
    overflow-x: auto; font-size: 11.5px; line-height: 1.6;
  }
  :global(.md blockquote) {
    margin: 10px 0; padding: 6px 12px;
    border-left: 4px solid var(--ember); color: #5c4a2e;
    background: #ece0c2;
  }
  :global(.md a) { color: #0b6e8a; text-decoration: underline dotted; }
  :global(.md hr) { border: none; border-top: 1px dashed #cbb98f; margin: 14px 0; }
  :global(.md table) { border-collapse: collapse; margin: 10px 0; width: 100%; font-size: 12px; }
  :global(.md th), :global(.md td) { border: 1px solid #cbb98f; padding: 4px 8px; text-align: left; }
  :global(.md th) { background: #e4d6b4; }
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
  .star {
    color: var(--amber);
    animation: blink 1.1s steps(2, jump-none) infinite;
  }
  @keyframes blink { 50% { opacity: 0.15; } }
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 2px solid #322640;
    margin: 2px 0 4px;
    flex-wrap: wrap;
  }
  .tab {
    font-family: "Press Start 2P", monospace;
    font-size: 9px;
    background: transparent;
    color: var(--dim);
    border: 2px solid transparent;
    border-bottom: none;
    padding: 8px 12px 7px;
    cursor: pointer;
    letter-spacing: 1px;
  }
  .tab:hover { color: var(--cream); }
  .tab.on {
    color: var(--bg);
    background: var(--amber);
    border-color: var(--amber);
    /* the lit tab is the door that is open — amber like the OPEN sign,
       and it physically sits on the seam so the room below belongs to it */
    box-shadow: 0 2px 0 var(--amber);
  }
  footer {
    display: flex;
    justify-content: space-between;
    color: var(--dim);
    font-size: 11px;
    margin-top: 18px;
  }
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(10, 6, 16, 0.78);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    z-index: 40;
  }
  .plate {
    width: min(860px, 96vw);
    max-height: 88vh;
    overflow: auto;
    background: var(--panel);
    border: 3px solid var(--amber);
    padding: 12px 16px 18px;
  }
  .plate-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .plate-head h2 {
    font-family: "Press Start 2P", monospace;
    font-size: 10px; color: var(--cream); margin: 0; flex: 1; line-height: 1.5;
  }
  .stamp {
    font-family: "Press Start 2P", monospace; font-size: 8px;
    color: var(--dim); border: 1px solid var(--dim); padding: 4px 6px; white-space: nowrap;
  }
  .stamp.gold { color: #ffd94a; border-color: #ffd94a; }
  .plate-actions { display: flex; gap: 6px; }
  .plate-actions button {
    background: none; border: 1px solid var(--dim); color: var(--cream);
    font: inherit; font-size: 11px; cursor: pointer; padding: 3px 8px;
  }
  .plate-actions button:hover { border-color: var(--amber); color: var(--amber); }
  .plate article.md { margin-top: 10px; }
  @media (max-width: 520px) {
    h1 { font-size: 12px; }
    .tab { font-size: 8px; padding: 7px 8px 6px; }
  }
</style>
