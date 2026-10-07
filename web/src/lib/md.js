// The plate, not the prep counter: markdown arrives, a rendered dish goes
// out. The rule is never raw markdown on this site — a reader came to read,
// not to parse asterisks.
//
// Sanitization is structural, not a filter list: the ENTIRE source is HTML-
// escaped first, so no tag can exist in the output unless this file writes
// it. The only href that survives is https/http — a javascript: URL is not
// a link, it is a trap with a label on it.

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
}

function inline(s) {
  // s is already escaped. Order: code spans first (so emphasis inside
  // `code` stays literal), then bold, then italic, then links.
  let out = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  // one level of nested parens inside the URL, exactly as markdown's
  // simplest form allows — so `evil(1)` inside a refused link dies whole,
  // brackets and all: a refusal that leaves `)` behind is raw markdown
  // wearing a trenchcoat, and this file has one rule.
  out = out.replace(/\[((?:[^\[\]]|\[[^\]]*\])*)\]\(((?:[^()\s]|\([^()\s]*\))*)\)/g,
                   (m, text, hrefRaw) => {
    const href = hrefRaw.replace(/&quot;/g, '"');
    if (!/^https?:\/\//i.test(href)) return text; // refused outright, whole
    return `<a href="${esc(href)}" rel="noopener noreferrer" target="_blank">${text}</a>`;
  });
  return out;
}

export function renderMarkdown(src) {
  if (!src) return '';
  const lines = esc(String(src)).split('\n');
  const out = [];
  let para = [], list = null, quote = [], code = null, table = null;

  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const flushList = () => {
    if (list) { out.push(`<${list.tag}>` + list.items.map(i => `<li>${inline(i)}</li>`).join('') + `</${list.tag}>`); list = null; }
  };
  const flushQuote = () => { if (quote.length) { out.push(`<blockquote>${inline(quote.join(' '))}</blockquote>`); quote = []; } };
  const flushTable = () => {
    if (!table) return;
    // rows are arrays of cells by now — the separator row is the row whose
    // EVERY cell is dashes-and-colons, not a string test against an array
    // (a `[array].test` stringifies with commas and matches nothing, which
    // is how `|---|---|` marched straight into the tbody in review).
    const isSep = (row) => Array.isArray(row) && row.length > 0 &&
                           row.every((c) => /^:?-{1,}:?$/.test(c));
    const head = table[0];
    const body = table.slice(isSep(table[1]) ? 2 : 1);
    let h = `<table><thead><tr>` + head.map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>';
    for (const row of body) h += '<tr>' + row.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
    out.push(h + '</tbody></table>'); table = null;
  };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); flushTable(); };

  for (let raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (code !== null) {
      if (line.trim() === '```') { out.push(`<pre class="code">${code.join('\n')}</pre>`); code = null; }
      else code.push(line.replace(/^`{3}/, ''));
      continue;
    }
    if (line.trim().startsWith('```')) { flushAll(); code = [line.replace(/^.*?```/, '')].filter(x => x !== ''); continue; }
    const tr = line.match(/^\s*\|(.+)\|\s*$/);
    if (tr) { flushPara(); flushList(); flushQuote(); (table ||= []).push(tr[1].split('|').map(c => c.trim())); continue; }
    flushTable();
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushAll(); const lvl = Math.min(h[1].length, 4); out.push(`<h${lvl + 2}>${inline(h[2])}</h${lvl + 2}>`); continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flushAll(); out.push('<hr/>'); continue; }
    const q = line.match(/^&gt;\s?(.*)$/);
    if (q) { flushPara(); flushList(); quote.push(q[1]); continue; }
    flushQuote();
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ul) { flushPara(); if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; } list.items.push(ul[1]); continue; }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) { flushPara(); if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; } list.items.push(ol[1]); continue; }
    flushList();
    if (!line.trim()) { flushPara(); continue; }
    para.push(line.trim());
  }
  if (code !== null) out.push(`<pre class="code">${code.join('\n')}</pre>`);
  flushAll();
  return out.join('\n');
}
