<script>
  // The cook's books: what the GPU is doing right now, what today cost,
  // and what each pot spent. Every number here is one the daemon actually
  // measured — the ledger panel is required never to lie, so anything the
  // server cannot confirm shows as "—" rather than a confident zero.
  let { snap, stats } = $props();

  function n(v) { return v == null ? '—' : v.toLocaleString(); }

  let gate = $derived(snap?.gate || {});
  let today = $derived(snap?.today || {});
  let days = $derived(stats?.days || []);
  let gens = $derived(stats?.generators || []);

  let maxTokens = $derived(Math.max(1, ...days.map(d => d.input_tokens + d.output_tokens)));

  let gpuLine = $derived.by(() => {
    if (gate.stale) return { text: 'daemon detached — last known state', cls: 'warn' };
    if (!gate.ledger_up) return { text: 'blind — the ledger cannot be reached', cls: 'warn' };
    if (gate.state === 'ACTIVE_INFER') return { text: 'busy — someone is generating right now', cls: 'busy' };
    if (gate.ledger_idle_s != null) return { text: `idle — nobody has asked the GPU for ${Math.round(gate.ledger_idle_s)}s`, cls: 'idle' };
    return { text: 'idle', cls: 'idle' };
  });
</script>

<section class="room">
  <h2>the gpu right now</h2>
  <div class="badge">
    <b class={gpuLine.cls}>{gate.state || 'OFFLINE'}</b>
    <span class="why">{gpuLine.text}</span>
  </div>
  <dl>
    <div><dt>gpu idle for</dt><dd>{gate.ledger_idle_s != null ? `${Math.round(gate.ledger_idle_s)}s` : '—'}</dd></div>
    <div><dt>on the gpu</dt><dd>{(gate.cooking || []).join(', ') || gate.ledger_active || 'nobody'}</dd></div>
    <div><dt>our own traffic</dt><dd>{gate.ledger_own || 0}</dd></div>
    <div><dt>listening to bytes</dt><dd class:warn={!gate.wire_up}>{gate.wire_up ? 'yes' : 'deaf'}</dd></div>
    <div><dt>quiet clock</dt><dd>{gate.quiet_s != null ? `${Math.round(gate.quiet_s)}s / ${(gate.ready_in_s || 4).toFixed(0)}s` : '—'}</dd></div>
  </dl>
</section>

<section class="room">
  <h2>today's bill</h2>
  <dl>
    <div><dt>tokens out</dt><dd>{n(today.output_tokens)}</dd></div>
    <div><dt>tokens in</dt><dd>{n(today.input_tokens)}</dd></div>
    <div><dt>gpu time</dt><dd>{today.gpu_seconds ?? '—'}s</dd></div>
    <div><dt>stages</dt><dd>{n(today.stages)}</dd></div>
    <div><dt>served ★</dt><dd>{n(today.published ?? 0)}</dd></div>
    <div><dt>pre-empts</dt><dd>{n(today.preemptions)}</dd></div>
    <div><dt>avg ttft</dt><dd>{today.avg_ttft_ms ? `${today.avg_ttft_ms}ms` : '—'}</dd></div>
    <div><dt>disk used</dt><dd>{snap?.disk ? (snap.disk.bytes / 1024 / 1024).toFixed(1) : '—'} MB</dd></div>
  </dl>
  <p class="plain">ttft = time to the first token a stage waited for. pre-empts =
    times the kitchen dropped its own pot because the GPU was asked for something.</p>
</section>

<section class="room">
  <h2>tokens by day <span class="sub">(in dim, out bright · ★ days that served)</span></h2>
  <div class="chart">
    {#each days as d (d.day)}
      <div class="col" title={`${d.day}: ${d.output_tokens.toLocaleString()} out / ${d.input_tokens.toLocaleString()} in · ${d.gpu_seconds}s gpu · ${d.published} served`}>
        <span class="star">{#if d.published > 0}★{/if}</span>
        <span class="stack">
          <i class="out" style="height:{(d.output_tokens / maxTokens) * 100}%"></i>
          <i class="in" style="height:{(d.input_tokens / maxTokens) * 100}%"></i>
        </span>
        <em>{d.day.slice(8)}</em>
      </div>
    {:else}
      <p class="dim">no days with spent GPU time yet</p>
    {/each}
  </div>
</section>

<section class="room">
  <h2>who cooked what</h2>
  <table>
    <thead><tr><th>pot</th><th>stages</th><th>tokens out</th><th>gpu sec</th><th>pre-empts</th></tr></thead>
    <tbody>
      {#each gens as g (g.generator)}
        <tr><td class="gen">{g.generator}</td><td>{g.stages}</td><td>{g.output_tokens.toLocaleString()}</td><td>{g.gpu_seconds}</td><td>{g.preemptions}</td></tr>
      {:else}
        <tr><td colspan="5" class="dim">nothing spent yet</td></tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  .room { margin: 14px 0; }
  h2 { font-family: "Press Start 2P", monospace; font-size: 11px; color: var(--amber); margin: 4px 0 8px; }
  .sub { font-family: ui-monospace, monospace; font-size: 10px; color: var(--dim); }
  .badge {
    display: flex; align-items: center; gap: 10px;
    background: var(--panel); border: 2px solid #322640; padding: 6px 10px; flex-wrap: wrap;
  }
  .badge b { font-family: "Press Start 2P", monospace; font-size: 10px; }
  .badge b.idle { color: var(--green); }
  .badge b.busy { color: var(--amber); }
  .badge b.warn { color: var(--red); }
  .badge .why { color: var(--dim); font-size: 11px; }
  dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 4px; margin: 8px 0 0;
  }
  dl > div { background: var(--panel); border: 2px solid #322640; padding: 4px 8px; min-width: 0; }
  dt { font-size: 9px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  dd { margin: 0; font-family: "Press Start 2P", monospace; font-size: 11px; color: var(--cream); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  dd.warn { color: var(--red); }
  .plain { color: #55506a; font-size: 10px; margin: 8px 2px 0; }
  .chart {
    display: flex; align-items: flex-end; gap: 6px;
    background: var(--panel); border: 2px solid #322640;
    padding: 10px 10px 4px; height: 150px;
  }
  .col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; height: 100%; justify-content: flex-end; min-width: 0; }
  .star { color: #ffd94a; font-size: 9px; height: 10px; }
  .stack { width: 100%; height: 110px; display: flex; flex-direction: column; justify-content: flex-end; }
  .stack i { display: block; width: 100%; }
  .stack .out { background: var(--ember); }
  .stack .in { background: #3a2f55; }
  .col em { font-style: normal; font-size: 9px; color: var(--dim); }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 2px solid #322640; font-size: 11px; }
  th { text-align: left; font-size: 9px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; padding: 5px 8px; border-bottom: 2px solid #322640; }
  td { padding: 5px 8px; border-bottom: 1px solid #2b2138; color: var(--cream); }
  td.gen { color: var(--cyan); }
  .dim { color: #55506a; font-size: 11px; }
</style>
