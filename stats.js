// Statistik-Tab: KPIs und Graphen über die KI-Nutzung. Reines SVG, keine Libraries.
(() => {
  // Farbe folgt dem Modell, nie dem Rang: feste Zuordnung in der validierten Slot-Reihenfolge.
  const FAMILY_COLOR = { Opus: 'var(--s1)', Sonnet: 'var(--s2)', Fable: 'var(--s3)', Haiku: 'var(--s4)',
                         GPT: 'var(--s5)', Qwen: 'var(--s6)', Andere: 'var(--s-other)' };
  const FAMILY_ORDER = Object.keys(FAMILY_COLOR);
  const WEEKDAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
  const state = { days: +(localGet('stats.days') ?? 30), metric: localGet('stats.metric') || 'cost', data: null };

  function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
  function localSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // Zahlen: Tausender mit ', Daten TT.MM.
  const int = n => Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, "'");
  const usd = n => n == null ? '–' : n >= 100 ? `$${int(n)}` : `$${n.toFixed(2)}`;
  const compact = n => n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : int(n);
  const ddmm = iso => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
  const dur = s => s == null ? '–' : s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
  const fmtMetric = (m, v) => m === 'cost' ? usd(v) : compact(v);

  function delta(cur, prev) {
    if (prev == null || !prev) return '';
    const pct = (cur - prev) / prev * 100;
    const arrow = pct >= 0 ? '▲' : '▼';
    return `<span class="delta" title="Vorperiode: ${esc(prev)}">${arrow} ${Math.abs(pct).toFixed(0)} %</span> vs. Vorperiode`;
  }

  // ---------- Tooltip ----------
  const tip = $('tip');
  function showTip(e, html) {
    tip.innerHTML = html; tip.style.opacity = 1;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  }
  const hideTip = () => { tip.style.opacity = 0; };
  document.addEventListener('mousemove', e => {
    const t = e.target.closest?.('[data-tip]');
    if (t && $('statsView').contains(t)) showTip(e, decodeURIComponent(t.dataset.tip)); else hideTip();
  });
  const tipAttr = html => `data-tip="${encodeURIComponent(html)}"`;

  // ---------- KPIs ----------
  function renderKpis(k) {
    const tiles = [
      ['API-Äquivalent', usd(k.cost[0]), delta(k.cost[0], k.cost[1]) || `≈ ${usd(k.cost[0] / (state.days || state.data.daily.length || 1))} pro Tag`],
      ['Cache-Hit-Rate', `${(k.cache_hit * 100).toFixed(1)} %`, `${usd(k.saved)} gespart ggü. ohne Cache`],
      ['Prompts', int(k.prompts[0]), delta(k.prompts[0], k.prompts[1]) || `${int(k.sessions[0])} Sessions`],
      ['Output-Tokens', compact(k.output[0]), delta(k.output[0], k.output[1]) || `${int(k.output[0])} Tokens`],
      ['Aktive Agent-Zeit', `${k.active_h[0]} h`, delta(k.active_h[0], k.active_h[1]) || `Ø ${k.parallel} Agents gleichzeitig`],
      ['Agent wartet auf dich', dur(k.wait_median[0]), k.wait_median[1] != null
        ? `Median, Vorperiode ${dur(k.wait_median[1])}` : `Median über ${int(k.wait_n)} Antworten`],
    ];
    $('kpis').innerHTML = tiles.map(([l, v, s]) =>
      `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join('');
  }

  // ---------- Gestapelte Tagesbalken ----------
  function renderDaily(d) {
    const fams = FAMILY_ORDER.filter(f => d.families.includes(f));
    const m = state.metric, rows = d.daily;
    const totals = rows.map(r => fams.reduce((s, f) => s + (r[`${m}_${f}`] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const W = 1100, H = 240, L = 48, B = 22, T = 8, plotW = W - L - 4, plotH = H - B - T;
    const bw = plotW / rows.length, gap = Math.min(4, bw * 0.25);
    const y = v => T + plotH - v / max * plotH;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Verlauf pro Tag">`;
    for (const t of ticks(max)) svg += `<line class="grid" x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${fmtMetric(m, t)}</text>`;
    const labelEvery = Math.ceil(rows.length / 12);
    rows.forEach((r, i) => {
      const x = L + i * bw + gap / 2, w = Math.max(1, bw - gap);
      let acc = 0, segs = '';
      const present = fams.filter(f => r[`${m}_${f}`] > 0);
      present.forEach((f, j) => {
        const v = r[`${m}_${f}`], h = v / max * plotH, top = j === present.length - 1;
        const y0 = y(acc + v), hh = Math.max(0, h - (j > 0 ? 2 : 0)); // 2px Fuge zwischen Segmenten
        segs += top && hh > 4 ? roundTop(x, y0, w, hh, Math.min(4, w / 2), FAMILY_COLOR[f])
                              : `<rect x="${x}" y="${y0}" width="${w}" height="${hh}" fill="${FAMILY_COLOR[f]}"/>`;
        acc += v;
      });
      const tipHtml = `<b>${ddmm(r.date)}</b> · ${fmtMetric(m, totals[i])}` +
        fams.filter(f => r[`${m}_${f}`] > 0).reverse().map(f =>
          `<div class="row"><span><i style="background:${FAMILY_COLOR[f]}"></i>${f}</span><span>${fmtMetric(m, r[`${m}_${f}`])}</span></div>`).join('') +
        `<div class="row"><span>Prompts</span><span>${int(r.prompts)}</span></div>`;
      svg += `<g>${segs}<rect class="hit" x="${L + i * bw}" y="${T}" width="${bw}" height="${plotH}" ${tipAttr(tipHtml)}/></g>`;
      if (i % labelEvery === 0) svg += `<text x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${ddmm(r.date)}</text>`;
    });
    $('dailyChart').innerHTML = svg + '</svg>';
    $('dailyLegend').innerHTML = fams.map(f => `<span><i style="background:${FAMILY_COLOR[f]}"></i>${f}</span>`).join('');
  }

  function roundTop(x, y, w, h, r, fill) {
    return `<path d="M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z" fill="${fill}"/>`;
  }
  function niceMax(v) { const p = 10 ** Math.floor(Math.log10(v)); return Math.ceil(v / p / (v / p > 5 ? 2 : 1)) * p * (v / p > 5 ? 2 : 1); }
  function ticks(max) { return [0, max / 4, max / 2, max * 3 / 4, max]; }

  // ---------- Horizontale Balken ----------
  function hbars(el, items, val, label, sub) {
    const max = Math.max(1e-9, ...items.map(val));
    $(el).innerHTML = items.length ? items.map(it => `<div class="hbar" ${tipAttr(`<b>${esc(label(it))}</b>${sub ? ' · ' + esc(sub(it)) : ''}<div class="row"><span>USD</span><span>${usd(it.cost)}</span></div><div class="row"><span>Output</span><span>${compact(it.output)}</span></div><div class="row"><span>Prompts</span><span>${int(it.prompts)}</span></div>`)}>
        <span class="n">${esc(label(it))}${sub ? `<small>${esc(sub(it))}</small>` : ''}</span>
        <span class="track"><div class="fill" style="width:${val(it) / max * 100}%"></div></span>
        <span class="v">${usd(val(it))}</span></div>`).join('') : '<p class="muted">Keine Daten</p>';
  }

  function renderTools(items) {
    const max = Math.max(1, ...items.map(t => t.n));
    $('toolsChart').innerHTML = items.map(t => `<div class="hbar" ${tipAttr(`<b>${esc(t.name)}</b> · ${int(t.n)} Aufrufe`)}>
      <span class="n">${esc(t.name.replace('MCP: plugin_', 'MCP: '))}</span>
      <span class="track"><div class="fill" style="width:${t.n / max * 100}%"></div></span>
      <span class="v">${int(t.n)}</span></div>`).join('');
  }

  // ---------- Heatmap ----------
  function renderHeat(heat) {
    const max = Math.max(1, ...heat.flat());
    const cw = 22, ch = 20, L = 26, T = 4, W = L + 24 * cw, H = T + 7 * ch + 18;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Prompts nach Wochentag und Stunde">`;
    heat.forEach((row, d) => {
      svg += `<text x="${L - 6}" y="${T + d * ch + ch / 2 + 4}" text-anchor="end">${WEEKDAYS[d]}</text>`;
      row.forEach((v, h) => {
        const step = v === 0 ? 0 : Math.min(5, 1 + Math.floor(v / max * 4.999));
        svg += `<rect x="${L + h * cw + 1}" y="${T + d * ch + 1}" width="${cw - 2}" height="${ch - 2}" rx="3" fill="var(--seq${step})" ${tipAttr(`<b>${WEEKDAYS[d]} ${h}–${h + 1} Uhr</b> · ${int(v)} Prompts`)}/>`;
      });
    });
    for (let h = 0; h < 24; h += 3) svg += `<text x="${L + h * cw + cw / 2}" y="${H - 4}" text-anchor="middle">${h}</text>`;
    $('heatChart').innerHTML = svg + '</svg>';
  }

  // ---------- Parallelität (Linien) ----------
  function renderParallel(rows) {
    const series = [['peak', 'Spitze', 'var(--s2)'], ['parallel', 'Ø', 'var(--s1)']];
    const max = niceMax(Math.max(2, ...rows.map(r => r.peak)));
    const W = 1100, H = 180, L = 48, B = 22, T = 8, plotW = W - L - 10, plotH = H - B - T;
    const x = i => L + (rows.length === 1 ? plotW / 2 : i / (rows.length - 1) * plotW);
    const y = v => T + plotH - v / max * plotH;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Parallele Agents">`;
    for (const t of ticks(max)) svg += `<line class="grid" x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${+t.toFixed(1)}</text>`;
    for (const [k, , color] of series) {
      const pts = rows.map((r, i) => `${x(i).toFixed(1)},${y(r[k]).toFixed(1)}`).join(' ');
      svg += `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    const labelEvery = Math.ceil(rows.length / 12), step = plotW / Math.max(1, rows.length - 1);
    rows.forEach((r, i) => {
      if (i % labelEvery === 0) svg += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${ddmm(r.date)}</text>`;
      svg += `<rect class="hit" x="${x(i) - step / 2}" y="${T}" width="${step}" height="${plotH}" ${tipAttr(`<b>${ddmm(r.date)}</b><div class="row"><span><i style="background:var(--s2)"></i>Spitze</span><span>${r.peak}</span></div><div class="row"><span><i style="background:var(--s1)"></i>Ø</span><span>${r.parallel}</span></div>`)}/>`;
    });
    $('parChart').innerHTML = svg + '</svg>';
    $('parLegend').innerHTML = series.map(([, l, c]) => `<span><i class="line" style="background:${c}"></i>${l}</span>`).join('');
  }

  function renderSources(src) {
    $('sourcesTable').innerHTML = `<table class="src"><tr><th>Quelle</th><th>USD</th><th>Output-Tokens</th><th>Prompts</th></tr>` +
      src.map(s => `<tr><td>${esc(s.name)}</td><td>${s.name === 'Codex' ? 'kein Preis' : usd(s.cost)}</td><td>${int(s.output)}</td><td>${int(s.prompts)}</td></tr>`).join('') + '</table>';
  }

  function render() {
    const d = state.data; if (!d) return;
    document.querySelectorAll('#range button').forEach(b => b.classList.toggle('on', +b.dataset.days === state.days));
    document.querySelectorAll('#metric button').forEach(b => b.classList.toggle('on', b.dataset.m === state.metric));
    renderKpis(d.kpi);
    renderDaily(d);
    $('groupsPanel').hidden = !d.groups.length;
    hbars('groupsChart', d.groups, it => it.cost, it => it.name);
    hbars('projectsChart', d.projects, it => it.cost, it => it.name, it => it.group);
    renderHeat(d.heat);
    renderTools(d.tools);
    renderParallel(d.daily);
    renderSources(d.sources);
    const upd = d.updated ? new Date(d.updated * 1000).toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' }) : '–';
    $('statsNote').textContent = d.building && !d.updated ? 'Index wird aufgebaut …'
      : `Vollständig ab ${ddmm(d.earliest)}${d.earliest.slice(0, 4)} · Index ${upd}`;
  }

  async function load() {
    try {
      state.data = await (await fetch(`/api/stats?days=${state.days}`)).json();
      render();
      if (state.data.building && !state.data.updated) setTimeout(load, 3000);
    } catch { $('statsNote').textContent = 'Server weg?'; }
  }

  $('range').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.days = +b.dataset.days; localSet('stats.days', state.days); load();
  });
  $('metric').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.metric = b.dataset.m; localSet('stats.metric', state.metric); render();
  });

  // ---------- Tabs ----------
  function route() {
    const tab = location.hash === '#stats' ? 'stats' : 'inbox';
    $('inboxView').hidden = tab !== 'inbox';
    $('statsView').hidden = tab !== 'stats';
    $('stats').hidden = tab !== 'inbox';
    document.querySelectorAll('.tabs a').forEach(a => a.classList.toggle('on', a.dataset.tab === tab));
    if (tab === 'stats') load();
  }
  addEventListener('hashchange', route);
  route();
})();
