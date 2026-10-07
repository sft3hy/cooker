<script>
  // The pantry: every plate the kitchen has produced, on shelves.
  // PUBLISHED things get the gold shelf at eye level — they passed the
  // judge — candidates simmer below, and the shelf labels say what each
  // word means because this tab is where a stranger should understand
  // what the machine has been making.
  import { onMount } from 'svelte';

  let { refreshKey = 0, openArtifact, openDigest } = $props();

  let products = $state([]);
  let counts = $state({ published: 0, candidate: 0 });
  let filter = $state('all');
  let note = $state('opening the pantry…');
  let err = $state(false);

  const GEN_COLOR = { research: 'var(--cyan)', brainstorm: 'var(--amber)', digest: 'var(--green)' };

  const FILTERS = [
    ['all', 'ALL'],
    ['published', '★ SERVED'],
    ['research', 'RESEARCH'],
    ['brainstorm', 'BRAINSTORM'],
  ];

  async function load() {
    try {
      const r = await fetch('./api/products');
      if (!r.ok) throw new Error(`${r.status}`);
      const d = await r.json();
      products = d.products;
      counts = d.counts;
      err = false;
      note = products.length
        ? `${products.length} plates on the shelves`
        : 'the shelves are bare — nothing cooked yet';
    } catch (e) {
      err = true;
      note = 'cannot reach the pantry';
    }
  }

  $effect(() => {
    if (!refreshKey) return;   // eslint hint: re-run on event bumps only after first mount
    load();
  });

  onMount(load);

  let shown = $derived(
    filter === 'all' ? products
      : filter === 'published' ? products.filter(p => p.status === 'PUBLISHED')
      : products.filter(p => p.generator === filter)
  );

  // "synthesize: why does x" reads as noise on a shelf; the shelf knows it
  // made an article, so show the subject and keep `kind` as the small print.
  function topic(title) {
    return title.replace(/^(synthesize|digest|research|plan|extract|critique|brainstorm|generate|draft|review|evaluate):\s*/i, '');
  }
  function kind(p) {
    const m = p.title.match(/^([a-z]+):/i);
    return m ? m[1] : (p.kind || '');
  }
  function fmtBytes(b) {
    if (b == null) return 'gone';
    return b > 2048 ? `${(b / 1024).toFixed(1)} KB` : `${b} B`;
  }
</script>

<div class="topbar">
  <div class="filters">
    {#each FILTERS as [key, label] (key)}
      <button class="chip" class:on={filter === key} onclick={() => { filter = key; }}>
        {label}{#if key === 'published' && counts.published} {counts.published}{/if}
      </button>
    {/each}
  </div>
  <button class="chip menu" onclick={openDigest}>📋 TODAY'S MENU</button>
</div>

<p class="note" class:err>{note}</p>

<div class="shelves">
  {#each shown as p (p.id)}
    <button class="jar" class:gold={p.status === 'PUBLISHED'}
            onclick={() => openArtifact(p.id, p)}
            aria-label={`read: ${topic(p.title)}`}>
      <svg width="26" height="34" viewBox="0 0 13 17" shape-rendering="crispEdges" aria-hidden="true">
        <rect x="4" y="0" width="5" height="2" fill={p.status === 'PUBLISHED' ? '#ffd94a' : '#3a2f4d'} />
        <rect x="3" y="2" width="7" height="1" fill="#241c2e" />
        <rect x="3" y="3" width="7" height="13" fill={p.status === 'PUBLISHED' ? '#6b5417' : '#332845'} />
        <rect x="4" y="4" width="5" height="11" fill={GEN_COLOR[p.generator] || '#9fe8ea'} opacity="0.85" />
        <rect x="4" y="8" width="5" height="4" fill="#f2e7cf" />
      </svg>
      <span class="body">
        <span class="title">{topic(p.title)}</span>
        <span class="meta">
          {p.generator} · {kind(p)} · {fmtBytes(p.bytes)} · {p.day}
          {#if p.score != null}<em class="score">{p.score} ★</em>{/if}
          {#if p.status === 'PUBLISHED'}<em class="served">served</em>
          {:else}<em class="cand">in the pan</em>{/if}
        </span>
      </span>
    </button>
  {:else}
    {#if !err}
      <p class="dim">nothing on this shelf yet. the kitchen only cooks when the
        GPU is unclaimed — check back after a quiet hour.</p>
    {/if}
  {/each}
</div>

<style>
  .topbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip {
    font-family: "Press Start 2P", monospace; font-size: 8px;
    background: var(--panel); color: var(--dim);
    border: 2px solid #322640; padding: 7px 9px; cursor: pointer;
  }
  .chip.on { color: var(--bg); background: var(--amber); border-color: var(--amber); }
  .chip.menu:hover { border-color: var(--green); color: var(--green); }
  .note { color: var(--dim); font-size: 11px; margin: 10px 2px 2px; }
  .note.err { color: var(--red); }
  .shelves {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 8px;
    margin-top: 8px;
    padding: 10px;
    background: linear-gradient(180deg, transparent 0 92%, #2b2138 92% 100%);
    border: 2px solid #322640;
  }
  .jar {
    display: flex; gap: 10px; align-items: center; text-align: left;
    background: var(--panel); border: 2px solid #322640;
    padding: 8px 10px; cursor: pointer; color: var(--ink); font: inherit;
  }
  .jar:hover { border-color: var(--amber); }
  .jar svg { flex: 0 0 auto; image-rendering: pixelated; }
  .jar.gold { border-color: #6b5417; box-shadow: inset 0 0 0 1px #3d2f0d; }
  .body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .title { font-size: 12px; line-height: 1.35; }
  .meta { color: var(--dim); font-size: 10px; }
  .meta em { font-style: normal; }
  .score { color: var(--amber); }
  .served { color: #ffd94a; }
  .cand { color: var(--dim); }
  .dim { color: #55506a; font-size: 11px; }
</style>
