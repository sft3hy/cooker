<script>
  // The apron: the plain-language explanation, worn on the front.
  // Rule for this file: every sentence must be true, and it must be true
  // with no experience reading the code. No numbers invented here — the
  // measurements live in DISCOVERY.md and this room just tells the story.
</script>

<section class="room">
  <h2>what this kitchen is</h2>
  <p>Your Mac's graphics card (the GPU) runs a local AI service called
    <b>omlx</b>. Most of the day nobody is asking it for anything, and it
    sits there finished-with. This kitchen is a small worker that waits for
    those quiet stretches and uses the leftover effort to research topics,
    brainstorm ideas, and write little articles — all filed on this machine,
    never sent anywhere.</p>
</section>

<section class="room">
  <h2>the one rule</h2>
  <blockquote>"it should run when I'm using the computer, just not when I'm
    hitting omlx via anything."<br /><cite>— Sam, the owner, 2026-10-07</cite></blockquote>
  <p>Typing, browsing, compiling: the kitchen keeps cooking. The instant
    <em>anything</em> asks the GPU for something — you, OpenCode, any app —
    the kitchen drops its own work within a second (it costs you about a
    tenth of a second, measured) and waits for the quiet to come back.</p>
</section>

<section class="room">
  <h2>how it knows the oven is free</h2>
  <ul>
    <li><b>The ledger</b> — the AI service keeps its own tally: requests in
      flight, and seconds since its last piece of work. That dial is the
      judge. Watching the network cables instead was tried first, and failed:
      a browser admin page ticking over twice a second looked exactly like
      work forever, and the kitchen starved at an open oven.</li>
    <li><b>The wires</b> — bytes are now a tripwire, not a judge: they can
      interrupt a pot in progress, and if the ledger goes unreachable they
      fall back to judging by bytes (unexplained traffic = assume work).</li>
    <li><b>Being at the desk</b> — keyboard and mouse activity is written on
      the order ticket ("human present") but it closes nothing. That was the
      rule amendment above.</li>
    <li><b>Blind</b> — if the kitchen cannot see the GPU at all, it finishes
      what's in the pan and starts nothing new. Not-knowing is never quiet.</li>
  </ul>
</section>

<section class="room">
  <h2>what it cooks</h2>
  <ul>
    <li><b>Research</b> — takes a topic, plans it, searches, fetches pages,
      pulls out the facts, writes an article, then a harsh second model
      <em>judges</em> the writing. Score of <b>3.5 or better</b> or it never
      reaches the shelf — and lowering that bar to force a demo is forbidden.</li>
    <li><b>Brainstorm</b> — quick idea lists, same rules.</li>
    <li><b>The digest</b> — a short morning menu of everything the kitchen
      made, on the Pantry tab ("today's menu").</li>
  </ul>
  <p>Work is cut into small resumable stages, so when it has to stop
    mid-pot, it loses seconds — not hours.</p>
</section>

<section class="room">
  <h2>ordering your own dish</h2>
  <p>Write at the counter on the KITCHEN tab: a topic, and one of two
    kinds.</p>
  <ul>
    <li><b>DEEP-DIVE</b> — the kitchen hands your topic to
      <b>OpenCode</b> — the same assistant you drive in the terminal — as
      its own <em>new, separate</em> session, in its own tidy folder on
      this machine. It digs (a few minutes of real GPU), writes a brief,
      and the brief faces the same harsh judge as everything else. You can
      see that session in OpenCode's own session list; the kitchen never
      hides it, and never touches <em>your</em> sessions.</li>
    <li><b>RESEARCH</b> — the five-stage way above, cooked at home.</li>
  </ul>
  <p>The counter takes three pending orders at a time and says so politely
    when it won't take a fourth. While the kitchen is paused it won't take
    tickets at all — an order never unlocks a closed kitchen.</p>
</section>

<section class="room">
  <h2>reading the signs</h2>
  <ul>
    <li><span class="k open">OPEN / ready</span> — GPU quiet, pots filling.</li>
    <li><span class="k">quiet 3s of 4s</span> — waiting out the safety pause before starting work.</li>
    <li><span class="k">cooking — N stage(s) on</span> — real work on the GPU.</li>
    <li><span class="k">paused — someone is generating</span> you're back; dropped and waiting.</li>
    <li><span class="k">blind — cannot see the GPU</span> — the service is unreachable; it will not start anything new until it can see again.</li>
  </ul>
</section>

<section class="room">
  <h2>the rooms of this app</h2>
  <ul>
    <li><b>KITCHEN</b> (key 1) — the stove: pots, the gate sign, the live log.</li>
    <li><b>PANTRY</b> (key 2) — everything it made; gold jars passed the judge. Click any jar to read it.</li>
    <li><b>STATS</b> (key 3) — the books: tokens spent, GPU time, who cooked what.</li>
    <li><b>GUIDE</b> (key 4) — you are here.</li>
  </ul>
  <p class="fine">Everything runs on this machine. The kitchen's own calls
    are to your GPU and, for deep-dives, to your own local OpenCode server
    — which digs with whatever tools <em>you</em> configured for it (a web
    search if you gave it one). No third-party services, no cloud, no
    telemetry. The only GPU either one ever asks is yours.</p>
</section>

<style>
  .room { margin: 14px 0; }
  h2 { font-family: "Press Start 2P", monospace; font-size: 11px; color: var(--amber); margin: 4px 0 8px; }
  p, li { color: var(--cream); font-size: 12.5px; line-height: 1.6; }
  ul { padding-left: 18px; margin: 6px 0; }
  li { margin: 4px 0; }
  b { color: var(--cyan); }
  em { color: var(--amber); font-style: italic; }
  blockquote {
    margin: 8px 0; padding: 10px 12px;
    background: var(--panel); border-left: 4px solid var(--amber);
    font-size: 13px; color: var(--cream); line-height: 1.6;
  }
  cite { display: block; margin-top: 6px; color: var(--dim); font-style: normal; font-size: 11px; }
  .k {
    display: inline-block; font-family: "Press Start 2P", monospace; font-size: 8px;
    background: #16101e; color: var(--cyan); border: 1px solid #322640;
    padding: 3px 5px; margin-right: 6px; white-space: nowrap;
  }
  .k.open { color: var(--green); border-color: var(--green); }
  .fine { color: #55506a; font-size: 11px; border-top: 1px solid #322640; padding-top: 8px; }
</style>
