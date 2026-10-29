/*
 * Tela Painel: lê /api/gerenciador/busca/painel e desenha tudo em HTML/SVG, sem biblioteca
 * (a política de segurança do painel só aceita scripts do próprio servidor).
 * Todo texto vindo da API entra por textContent; os gráficos têm dica, foco por teclado e
 * visão em tabela, então nenhum valor depende só de cor ou do mouse.
 */
(() => {
  'use strict';
  const TZ = 'America/Sao_Paulo';
  const $ = (id) => document.getElementById(id);
  const nf = new Intl.NumberFormat('pt-BR');
  const fmt = (n) => nf.format(Math.round(n));
  const dec = (n, d) => Number(n).toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (n, d = 2) => 'US$ ' + dec(n, d);
  const STAGES = { discovery: 'Descoberta', dedup: 'Verificação no corpus', fetch: 'Extração textual', context: 'Análise de contexto', evidence: 'Evidência', persist: 'Gravação no corpus' };
  const SERIES = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)'];
  const MARK = 'var(--green)';
  const RUN_STATE = { running: ['Em andamento', 'blue'], completed: ['Concluída', 'green'], partial: ['Concluída com falhas', 'amber'] };
  const CATEGORY = { healthy: ['Saudáveis', 'green'], access_error: ['Falha de acesso', 'red'], review: ['Aguardam revisão', 'amber'], stale: ['Parados', 'amber'], invalid: ['Inválidos', 'red'], removed: ['Excluídos', 'gray'], inconclusive: ['Inconclusivos', 'gray'], no_text: ['Sem texto', 'amber'], no_news: ['Sem notícias', 'amber'], skipped: ['Cadastro alterado', 'gray'], error: ['Falha interna', 'red'] };

  let days = 7;
  let busy = false;
  let data = null;
  try { days = Number(localStorage.getItem('bm_panel_days')) || 7; } catch { /* sem armazenamento */ }
  if (![3, 7, 14, 30].includes(days)) days = 7;

  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const parts = (iso) => Object.fromEntries(new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  const dayMonth = (iso) => { const p = parts(iso); return `${p.day}/${p.month}`; };
  const clock = (iso) => { const p = parts(iso); return `${p.hour}:${p.minute}`; };
  function runLabel(run) {
    const p = parts(run.started_at);
    return run.trigger === 'scheduled' ? `${p.day}/${p.month} ${p.hour}h` : `${p.day}/${p.month} ${p.hour}:${p.minute}`;
  }
  function span(seconds) {
    const m = Math.round(seconds / 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
  }
  function until(iso) {
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return 'agora';
    return 'em ' + (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`);
  }
  const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);
  const stage = (run, s) => run.stages[s] || {};
  const failRate = (run, s) => { const f = stage(run, s).failed || 0; const t = f + (stage(run, s).succeeded || 0); return t ? (f / t) * 100 : null; };
  const failedTotal = (run) => Object.values(run.stages).reduce((a, s) => a + (s.failed || 0), 0);
  const tasksTotal = (run) => Object.values(run.stages).reduce((a, s) => a + sum(s), 0);

  /* ---------- dica (tooltip) ---------- */
  const tip = $('tip');
  function showTip(title, rows, x, y) {
    tip.replaceChildren(el('div', title, 'tip-title'));
    for (const row of rows) {
      const line = el('div', undefined, 'row');
      const name = el('span');
      if (row.color) { const key = el('i'); key.style.borderTopColor = row.color; name.append(key); }
      name.append(document.createTextNode(row.name));
      line.append(name, el('b', row.value));
      tip.append(line);
    }
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - box.width - 8)) + 'px';
    tip.style.top = Math.max(8, Math.min(y + 14, innerHeight - box.height - 8)) + 'px';
  }
  const hideTip = () => { tip.hidden = true; };
  function bindTip(target, build) {
    target.addEventListener('pointermove', (e) => build(e.clientX, e.clientY));
    target.addEventListener('pointerleave', hideTip);
    target.addEventListener('focus', () => { const b = target.getBoundingClientRect(); build(b.left + b.width / 2, b.top + 8); });
    target.addEventListener('blur', hideTip);
  }

  /* ---------- escalas ---------- */
  function niceMax(value) {
    if (value <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(value));
    const f = value / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  const compact = (v) => (v >= 1000 ? dec(v / 1000, v % 1000 ? 1 : 0) + ' mil' : dec(v, v < 10 && v % 1 ? 1 : 0));
  const M = { l: 46, r: 58, t: 16, b: 28 };

  function frame(body, max, fmtY) {
    const w = Math.max(300, body.clientWidth || 300);
    const h = 220;
    const plotW = w - M.l - M.r;
    const plotH = h - M.t - M.b;
    const root = svgEl('svg', { viewBox: `0 0 ${w} ${h}`, role: 'img' });
    const y = (v) => M.t + plotH - (v / max) * plotH;
    for (let i = 0; i <= 4; i += 1) {
      const v = (max / 4) * i;
      root.append(svgEl('line', { class: 'grid', x1: M.l, x2: w - M.r, y1: y(v), y2: y(v) }));
      root.append(svgEl('text', { x: M.l - 8, y: y(v) + 4, 'text-anchor': 'end' }, fmtY(v)));
    }
    return { root, w, h, plotW, plotH, y };
  }
  function xLabels(root, labels, xAt, plotW, h) {
    // Rótulos espaçados a partir do último, para o mais recente sempre aparecer sem colisão.
    const step = Math.max(1, Math.ceil(labels.length / Math.max(1, Math.floor(plotW / 74))));
    labels.forEach((label, i) => {
      if ((labels.length - 1 - i) % step === 0) root.append(svgEl('text', { x: xAt(i), y: h - 8, 'text-anchor': 'middle' }, label));
    });
  }

  /* ---------- gráficos ---------- */
  function barChart(body, items, { color = MARK, fmtValue = fmt, fmtY = compact } = {}) {
    const max = niceMax(Math.max(...items.map((i) => i.value), 1));
    const f = frame(body, max, fmtY);
    const band = f.plotW / items.length;
    const bw = Math.min(24, band * 0.7);
    const base = f.y(0);
    const xAt = (i) => M.l + band * i + band / 2;
    items.forEach((item, i) => {
      const top = f.y(item.value);
      const r = Math.min(4, base - top);
      const x = xAt(i) - bw / 2;
      const group = svgEl('g', { class: 'mark' });
      if (item.value > 0) {
        group.append(svgEl('path', { class: 'bar', fill: color, d: `M${x},${base} L${x},${top + r} Q${x},${top} ${x + r},${top} L${x + bw - r},${top} Q${x + bw},${top} ${x + bw},${top + r} L${x + bw},${base} Z` }));
      }
      const hit = svgEl('rect', { class: 'hit', x: M.l + band * i, y: M.t, width: band, height: f.plotH, tabindex: 0, 'aria-label': `${item.label}: ${fmtValue(item.value)}` });
      bindTip(hit, (cx, cy) => showTip(item.label, item.rows, cx, cy));
      group.append(hit);
      f.root.append(group);
    });
    const last = items[items.length - 1];
    f.root.append(svgEl('text', { class: 'end', x: xAt(items.length - 1), y: f.y(last.value) - 6, 'text-anchor': 'middle' }, fmtValue(last.value)));
    xLabels(f.root, items.map((i) => i.label), xAt, f.plotW, f.h);
    body.replaceChildren(f.root);
  }

  function lineChart(body, labels, series, { fmtY = compact, fmtValue = fmt, extra = () => [] } = {}) {
    const all = series.flatMap((s) => s.values.filter((v) => v !== null));
    // Piso minúsculo, não 1: o custo por notícia é da ordem de US$ 0,004 e ficaria achatado.
    const max = niceMax(Math.max(...all, 1e-9));
    const f = frame(body, max, fmtY);
    const n = labels.length;
    const xAt = (i) => (n === 1 ? M.l + f.plotW / 2 : M.l + (f.plotW * i) / (n - 1));
    const ends = [];
    series.forEach((s) => {
      let path = '';
      let pen = false;
      s.values.forEach((v, i) => {
        if (v === null) { pen = false; return; }
        path += `${pen ? 'L' : 'M'}${xAt(i)},${f.y(v)} `;
        pen = true;
      });
      f.root.append(svgEl('path', { d: path, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      const at = s.values.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0).pop();
      if (at !== undefined) {
        f.root.append(svgEl('circle', { cx: xAt(at), cy: f.y(s.values[at]), r: 4, fill: s.color, stroke: 'var(--paper)', 'stroke-width': 2 }));
        ends.push({ y: f.y(s.values[at]), at, text: s.fmt ? s.fmt(s.values[at]) : fmtValue(s.values[at]) });
      }
    });
    // Rótulo no fim só quando as séries se separam; senão a legenda e a dica carregam a leitura.
    const sorted = [...ends].sort((a, b) => a.y - b.y);
    const clear = sorted.every((e, i) => i === 0 || e.y - sorted[i - 1].y >= 14);
    if (clear) ends.forEach((e) => f.root.append(svgEl('text', { class: 'end', x: xAt(e.at) + 10, y: e.y + 4 }, e.text)));
    xLabels(f.root, labels, xAt, f.plotW, f.h);
    const cross = svgEl('line', { x1: 0, x2: 0, y1: M.t, y2: M.t + f.plotH, stroke: 'var(--line-strong)', 'stroke-width': 1, visibility: 'hidden' });
    f.root.append(cross);
    const overlay = svgEl('rect', { class: 'hit', x: M.l, y: M.t, width: f.plotW, height: f.plotH, tabindex: 0, 'aria-label': 'Percorrer os pontos do gráfico com as setas' });
    let index = n - 1;
    const point = (i, cx, cy) => {
      index = i;
      cross.setAttribute('x1', xAt(i)); cross.setAttribute('x2', xAt(i)); cross.setAttribute('visibility', 'visible');
      showTip(labels[i], [...series.map((s) => ({ color: s.color, name: s.name, value: s.values[i] === null ? '—' : (s.fmt ? s.fmt(s.values[i]) : fmtValue(s.values[i])) })), ...extra(i)], cx, cy);
    };
    overlay.addEventListener('pointermove', (e) => {
      const box = f.root.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * f.w;
      point(Math.max(0, Math.min(n - 1, Math.round(((px - M.l) / f.plotW) * (n - 1 || 1)))), e.clientX, e.clientY);
    });
    overlay.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
    overlay.addEventListener('focus', () => { const b = overlay.getBoundingClientRect(); point(index, b.left + (b.width * index) / Math.max(1, n - 1), b.top + 10); });
    overlay.addEventListener('blur', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
    overlay.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const b = overlay.getBoundingClientRect();
      const next = Math.max(0, Math.min(n - 1, index + (e.key === 'ArrowRight' ? 1 : -1)));
      point(next, b.left + (b.width * next) / Math.max(1, n - 1), b.top + 10);
    });
    f.root.append(overlay);
    body.replaceChildren(f.root);
  }

  function stackedChart(body, items, colors) {
    const max = niceMax(Math.max(...items.map((i) => i.parts.reduce((a, b) => a + b, 0)), 1));
    const f = frame(body, max, compact);
    const band = f.plotW / items.length;
    const bw = Math.min(24, band * 0.7);
    const xAt = (i) => M.l + band * i + band / 2;
    items.forEach((item, i) => {
      let top = f.y(0);
      const group = svgEl('g', { class: 'mark' });
      const nonZero = item.parts.map((v, k) => (v > 0 ? k : -1)).filter((k) => k >= 0);
      item.parts.forEach((v, k) => {
        if (v <= 0) return;
        const h = Math.max(2, (v / max) * f.plotH - 2);
        const y = top - h;
        const x = xAt(i) - bw / 2;
        const r = k === nonZero[nonZero.length - 1] ? Math.min(4, h) : 0;
        group.append(svgEl('path', { class: 'bar', fill: colors[k], d: `M${x},${top} L${x},${y + r} ${r ? `Q${x},${y} ${x + r},${y} L${x + bw - r},${y} Q${x + bw},${y} ${x + bw},${y + r}` : `L${x},${y} L${x + bw},${y}`} L${x + bw},${top} Z` }));
        top = y - 2;
      });
      const hit = svgEl('rect', { class: 'hit', x: M.l + band * i, y: M.t, width: band, height: f.plotH, tabindex: 0, 'aria-label': `${item.label}: ${item.rows.map((r) => `${r.name} ${r.value}`).join(', ')}` });
      bindTip(hit, (cx, cy) => showTip(item.label, item.rows, cx, cy));
      group.append(hit);
      f.root.append(group);
    });
    xLabels(f.root, items.map((i) => i.label), xAt, f.plotW, f.h);
    body.replaceChildren(f.root);
  }

  /* ---------- cartões ---------- */
  function table(head, rows) {
    const wrap = el('div', undefined, 'chart-table');
    const t = el('table');
    const tr = el('tr');
    head.forEach((h) => tr.append(el('th', h)));
    const thead = el('thead');
    thead.append(tr);
    t.append(thead);
    const tb = el('tbody');
    rows.forEach((r) => { const row = el('tr'); r.forEach((c) => row.append(el('td', String(c)))); tb.append(row); });
    t.append(tb);
    wrap.append(t);
    return wrap;
  }
  function card({ title, subtitle, legend, label, draw, head, rows, wide }) {
    const box = el('div', undefined, 'panel chart-card' + (wide ? ' wide' : ''));
    const headRow = el('div', undefined, 'chart-head');
    const titles = el('div');
    titles.append(el('h3', title), el('p', subtitle));
    const toggle = el('button', 'Ver como tabela');
    toggle.type = 'button';
    toggle.setAttribute('aria-pressed', 'false');
    toggle.onclick = () => {
      const on = box.classList.toggle('as-table');
      toggle.textContent = on ? 'Ver gráfico' : 'Ver como tabela';
      toggle.setAttribute('aria-pressed', String(on));
    };
    headRow.append(titles, toggle);
    const body = el('div', undefined, 'chart-body');
    body.setAttribute('aria-label', label);
    box.append(headRow, body);
    if (legend && legend.length > 1) {
      const key = el('div', undefined, 'legend');
      legend.forEach((l) => { const item = el('span'); const mark = el('i', undefined, l.box ? 'box' : ''); mark.style.borderColor = l.color; if (l.box) mark.style.background = l.color; item.append(mark, document.createTextNode(l.name)); key.append(item); });
      box.append(key);
    }
    box.append(table(head, rows));
    return { box, body, draw };
  }
  function drawAll(cards) {
    cards.forEach((c) => c.draw(c.body));
  }

  function tile(label, value, note, status) {
    const box = el('div', undefined, 'stat');
    box.append(el('span', label), el('strong', value));
    if (status) { const s = el('span', undefined, 'status ' + status[0]); s.append(document.createTextNode(status[1])); box.append(s); }
    if (note) box.append(el('small', note, 'tile-note'));
    return box;
  }
  const GOOD = '✓ ', WARN = '▲ ', BAD = '⚠ ';

  /* ---------- seções ---------- */
  function renderLast(d) {
    const holder = $('last');
    holder.className = 'panel last-card';
    holder.replaceChildren();
    if (!d.last) {
      holder.className = 'panel';
      holder.append(el('div', 'Ainda não há rodadas. Quando a primeira terminar, os números aparecem aqui.', 'empty'));
      $('last-tiles').replaceChildren();
      return;
    }
    const run = d.last;
    const hero = el('div', undefined, 'last-hero');
    const state = RUN_STATE[run.state] || [run.state, 'gray'];
    hero.append(el('h2', run.trigger === 'scheduled' ? `Rodada das ${parts(run.started_at).hour}h · ${dayMonth(run.started_at)}` : `Rodada manual · ${dayMonth(run.started_at)} ${clock(run.started_at)}`));
    const meta = el('div', undefined, 'last-meta');
    meta.append(el('span', state[0], 'badge ' + state[1]), el('span', span(run.duration_s) + (run.final ? '' : ' até agora'), 'badge gray'));
    hero.append(meta, el('div', fmt(run.persisted), 'hero-figure'), el('div', 'notícias gravadas no corpus', 'hero-label'));
    if (d.previous) {
      const diff = run.persisted - d.previous.persisted;
      const pct = d.previous.persisted ? (diff / d.previous.persisted) * 100 : null;
      const cls = diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat';
      const arrow = diff > 0 ? '▲ +' : diff < 0 ? '▼ ' : '= ';
      hero.append(el('div', `${arrow}${fmt(diff)}${pct === null ? '' : ` (${dec(pct, 0)}%)`} em relação à rodada de ${runLabel(d.previous)}`, 'delta ' + cls));
    }
    const funnel = el('div', undefined, 'funnel');
    funnel.append(el('h3', 'Do que foi encontrado ao que foi gravado'));
    const steps = [['URLs candidatas', sum(stage(run, 'dedup'))], ['Extraídas', stage(run, 'fetch').succeeded || 0], ['Analisadas', stage(run, 'context').succeeded || 0], ['Gravadas', run.persisted]];
    const top = Math.max(steps[0][1], 1);
    steps.forEach(([name, n], i) => {
      const row = el('div', undefined, 'step');
      const track = el('div', undefined, 'track');
      const fill = el('div', undefined, 'fill');
      fill.style.width = Math.max(0.5, (n / top) * 100) + '%';
      track.append(fill);
      const num = el('div', undefined, 'num');
      num.append(document.createTextNode(fmt(n)));
      if (i) num.append(el('small', dec(steps[i - 1][1] ? (n / steps[i - 1][1]) * 100 : 0, 0) + '%'));
      row.append(el('div', name), track, num);
      funnel.append(row);
    });
    holder.append(hero, funnel);
    const failed = failedTotal(run);
    const total = tasksTotal(run);
    $('last-tiles').replaceChildren(
      tile('Duração', span(run.duration_s), run.final ? 'da criação ao último resultado' : 'em andamento'),
      tile('Custo da rodada', money(run.cost_usd), run.persisted ? `${money(run.cost_usd / run.persisted, 4)} por notícia gravada` : 'nenhuma notícia gravada ainda'),
      tile('Links lidos', fmt(run.links), run.adaptive_skipped ? `${fmt(run.adaptive_skipped)} frios ficaram para a última rodada do dia` : `${fmt(run.links_done)} com descoberta concluída`),
      tile('Tarefas com falha', fmt(failed), total ? `${dec((failed / total) * 100, 1)}% de ${fmt(total)} tarefas` : ''),
      tile('Ocorrências', fmt(run.occurrences), `${fmt(run.unique_articles)} notícias únicas`),
    );
  }

  function renderNow(d) {
    const n = d.now;
    const tiles = [];
    if (n.open_run) {
      const queued = n.queues.reduce((a, q) => a + q.n, 0);
      tiles.push(tile('Rodada em andamento', 'Agora', `${fmt(queued)} tarefas ainda na fila`, ['good', GOOD + 'Processando']));
    } else if (n.next_round) {
      tiles.push(tile('Próxima rodada', clock(n.next_round), `${until(n.next_round)} · ${dayMonth(n.next_round)}`));
    } else {
      tiles.push(tile('Próxima rodada', '—', 'agendamento desligado', ['warn', WARN + 'Sem calendário']));
    }
    const perStage = {};
    n.queues.forEach((q) => { perStage[q.stage] = (perStage[q.stage] || 0) + q.n; });
    const queueNote = Object.entries(perStage).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([s, c]) => `${STAGES[s]} ${fmt(c)}`).join(' · ');
    tiles.push(tile('Em fila', fmt(n.queues.reduce((a, q) => a + q.n, 0)), queueNote || 'nada aguardando'));
    tiles.push(tile('Workers online', fmt(n.workers_online), 'com sinal nos últimos 2 min', n.workers_online ? ['good', GOOD + 'No ar'] : ['bad', BAD + 'Sem workers']));
    const free = n.disk_free_gb;
    const level = free >= 10 ? ['good', GOOD + 'Folgado'] : free >= 4 ? ['warn', WARN + 'Atenção'] : ['bad', BAD + 'Crítico'];
    const disk = tile('Disco livre', dec(free, 1) + ' GB', undefined, level);
    const meter = el('div', undefined, 'meter ' + (level[0] === 'good' ? '' : level[0]));
    const bar = el('span');
    bar.style.width = Math.min(100, (free / 40) * 100) + '%';
    meter.append(bar);
    disk.append(meter);
    tiles.push(disk);
    const c = n.catalog;
    tiles.push(tile('Links ativos', fmt(c.active_links), `${fmt(c.inactive_links)} inativos · ${fmt(c.cold_links)} frios`));
    const h = n.next_health;
    const nextHealth = h.active || h.recovery;
    tiles.push(tile('Próxima saúde', nextHealth ? clock(nextHealth) : '—', nextHealth ? `${until(nextHealth)} · ${dayMonth(nextHealth)}` : 'rotina desligada'));
    $('now').replaceChildren(...tiles);
  }

  function renderCharts(d) {
    const holder = $('charts');
    holder.replaceChildren();
    $('hist-lead').textContent = `Cada ponto é uma rodada de busca; período: últimos ${d.days} dias${d.pending_stats ? ` (${d.pending_stats} rodada(s) ainda sendo calculadas)` : ''}.`;
    if (!d.runs.length) { holder.append(el('div', 'Sem rodadas no período escolhido.', 'panel empty')); return; }
    const runs = d.runs;
    const labels = runs.map(runLabel);
    const rows = (run, extra) => [{ name: 'Gravadas', value: fmt(run.persisted) }, { name: 'Duração', value: span(run.duration_s) }, ...extra];
    const stagesShown = ['discovery', 'fetch', 'context'];
    const failSeries = stagesShown.map((s, i) => ({ name: STAGES[s], color: SERIES[i], values: runs.map((r) => (failRate(r, s) === null ? null : Number(failRate(r, s).toFixed(2)))), fmt: (v) => dec(v, 1) + '%' }));
    const cards = [
      card({
        title: 'Notícias gravadas por rodada', subtitle: 'Notícias novas que chegaram ao corpus',
        label: `Notícias gravadas por rodada, de ${labels[0]} a ${labels[labels.length - 1]}; última rodada ${fmt(runs[runs.length - 1].persisted)}`,
        head: ['Rodada', 'Gravadas', 'Duração', 'Custo'], rows: runs.map((r, i) => [labels[i], fmt(r.persisted), span(r.duration_s), money(r.cost_usd)]),
        draw: (body) => barChart(body, runs.map((r, i) => ({ label: labels[i], value: r.persisted, rows: rows(r, [{ name: 'Custo', value: money(r.cost_usd) }]) }))),
      }),
      card({
        title: 'Duração por rodada', subtitle: 'Minutos da criação da rodada ao último resultado',
        label: `Duração das rodadas em minutos; última ${span(runs[runs.length - 1].duration_s)}`,
        head: ['Rodada', 'Duração (min)'], rows: runs.map((r, i) => [labels[i], fmt(r.duration_s / 60)]),
        draw: (body) => lineChart(body, labels, [{ name: 'Duração', color: MARK, values: runs.map((r) => Math.round(r.duration_s / 60)), fmt: (v) => `${fmt(v)} min` }]),
      }),
      card({
        title: 'Custo por notícia gravada', subtitle: 'Dólares gastos na análise, divididos pelas notícias gravadas',
        label: 'Custo por notícia gravada em dólares',
        head: ['Rodada', 'Custo total', 'Gravadas', 'Custo por notícia'], rows: runs.map((r, i) => [labels[i], money(r.cost_usd), fmt(r.persisted), r.persisted ? money(r.cost_usd / r.persisted, 4) : '—']),
        draw: (body) => lineChart(body, labels, [{ name: 'Custo por notícia', color: MARK, values: runs.map((r) => (r.persisted ? Number((r.cost_usd / r.persisted).toFixed(5)) : null)), fmt: (v) => money(v, 4) }], { fmtY: (v) => (v === 0 ? '0' : dec(v, v < 0.01 ? 4 : 3)) }),
      }),
      card({
        title: 'Falhas por etapa', subtitle: 'Porcentagem das tarefas de cada etapa que falharam',
        legend: failSeries.map((s) => ({ name: s.name, color: s.color })),
        label: 'Taxa de falha por etapa em cada rodada',
        head: ['Rodada', ...failSeries.map((s) => s.name)], rows: runs.map((r, i) => [labels[i], ...failSeries.map((s) => (s.values[i] === null ? '—' : dec(s.values[i], 1) + '%'))]),
        draw: (body) => lineChart(body, labels, failSeries, { fmtY: (v) => dec(v, v < 10 ? 1 : 0) + '%' }),
      }),
    ];
    if (d.corpus) {
      const grow = d.corpus.daily;
      const dayLabels = grow.map((x) => `${x.day.slice(8, 10)}/${x.day.slice(5, 7)}`);
      const gained = grow.reduce((a, x) => a + x.added, 0);
      cards.push(card({
        title: 'Crescimento do corpus', subtitle: `Total de notícias no corpus ao fim de cada dia · hoje ${fmt(d.corpus.total)} (+${fmt(gained)} em ${d.days} dias)`, wide: true,
        label: `Total de notícias no corpus: de ${fmt(grow[0].total)} a ${fmt(d.corpus.total)} em ${d.days} dias`,
        head: ['Dia', 'Entraram no dia', 'Pelo buscador', 'Total no corpus'], rows: grow.map((x, i) => [dayLabels[i], fmt(x.added), fmt(x.crawler), fmt(x.total)]),
        draw: (body) => lineChart(body, dayLabels, [{ name: 'Total no corpus', color: MARK, values: grow.map((x) => x.total), fmt }], { extra: (i) => [{ name: 'Entraram no dia', value: fmt(grow[i].added) }, { name: 'Pelo buscador', value: fmt(grow[i].crawler) }] }),
      }));
    }
    cards.forEach((c) => holder.append(c.box));
    requestAnimationFrame(() => drawAll(cards));
    renderCharts.cards = cards;
  }

  function renderFailures(d) {
    const holder = $('failures');
    holder.replaceChildren();
    if (!d.failures.length) { holder.append(el('div', 'Nenhuma falha registrada no período.', 'empty')); return; }
    const wrap = el('div', undefined, 'table-wrap');
    const t = el('table', undefined, 'fail-table');
    const head = el('tr');
    ['Etapa', 'Motivo', 'Ocorrências', 'Parcela'].forEach((h) => head.append(el('th', h)));
    const thead = el('thead');
    thead.append(head);
    t.append(thead);
    const body = el('tbody');
    const top = d.failures[0].n;
    d.failures.forEach((f) => {
      const row = el('tr');
      const share = el('td');
      share.append(el('span', dec(f.pct, 1) + '%'));
      const bar = el('span', undefined, 'fail-bar');
      bar.style.width = Math.max(2, (f.n / top) * 100) + '%';
      share.append(bar);
      row.append(el('td', STAGES[f.stage] || f.stage), el('td', f.reason, 'reason'), el('td', fmt(f.n)), share);
      body.append(row);
    });
    t.append(body);
    wrap.append(t);
    holder.append(wrap);
  }

  function renderHealth(d) {
    const box = $('health-chart');
    box.replaceChildren();
    const daily = d.health.daily;
    const colors = SERIES;
    const names = ['Inativados', 'Transporte trocado', 'Reativados'];
    const items = daily.map((x) => {
      const label = `${x.day.slice(8, 10)}/${x.day.slice(5, 7)}`;
      return { label, parts: [x.inactivated, x.transport, x.reactivated], rows: [{ color: colors[0], name: names[0], value: fmt(x.inactivated) }, { color: colors[1], name: names[1], value: fmt(x.transport) }, { color: colors[2], name: names[2], value: fmt(x.reactivated) }, { name: 'Excluídos', value: fmt(x.deleted) }] };
    });
    const c = card({
      title: 'O que a saúde mudou por dia', subtitle: 'Links inativados, com o jeito de acesso trocado e reativados',
      legend: names.map((n, i) => ({ name: n, color: colors[i], box: true })),
      label: 'Ações da rotina de saúde por dia',
      head: ['Dia', ...names, 'Excluídos'], rows: daily.map((x, i) => [items[i].label, fmt(x.inactivated), fmt(x.transport), fmt(x.reactivated), fmt(x.deleted)]),
      draw: (body) => stackedChart(body, items, colors),
    });
    box.className = 'chart-card';
    box.style.padding = '0';
    box.style.border = '0';
    box.style.background = 'transparent';
    box.append(c.box);
    requestAnimationFrame(() => c.draw(c.body));
    renderHealth.card = c;
    const last = $('health-last');
    last.replaceChildren();
    const kinds = { active: 'Links ativos', recovery: 'Inativos com falha de acesso' };
    Object.entries(kinds).forEach(([kind, title]) => {
      const info = d.health.last[kind];
      const panel = el('div', undefined, 'panel');
      panel.append(el('h3', title));
      if (!info) { panel.append(el('p', 'Ainda sem verificação.', 'muted')); last.append(panel); return; }
      const state = info.status === 'completed' ? 'Concluída' : info.status === 'running' ? 'Em andamento' : info.status;
      panel.append(el('p', `${dayMonth(info.started_at)} ${clock(info.started_at)} · ${state} · ${fmt(info.total)} links`, 'muted'));
      const cats = el('div', undefined, 'cats');
      Object.entries(info.counts).sort((a, b) => b[1] - a[1]).forEach(([k, v]) => {
        const [name, tone] = CATEGORY[k] || [k, 'gray'];
        cats.append(el('span', `${fmt(v)} · ${name}`, 'badge ' + tone));
      });
      panel.append(cats);
      last.append(panel);
    });
  }

  function renderAudit(d) {
    const a = d.audit;
    const s = d.now.settings;
    const tiles = [];
    tiles.push(tile('Amostra auditada', fmt(a.sampled), `de ${fmt(a.discovery_reads)} leituras de descoberta (${dec((s.head_audit_rate || 0) * 100, 0)}% dos links)`, a.sampled ? null : ['warn', WARN + 'Sem amostra']));
    let loss;
    if (a.loss_pct === null) loss = tile('Perda estimada pelo corte', '—', 'a amostra auditada ainda não pegou um documento que o corte dispensaria', ['warn', WARN + 'Sem prova ainda']);
    else if (a.missed === 0) loss = tile('Perda estimada pelo corte', '0%', `${fmt(a.would_skip)} cortes auditados, nenhuma URL da janela perdida`, ['good', GOOD + 'Nenhuma perda']);
    else loss = tile('Perda estimada pelo corte', dec(a.loss_pct, a.loss_pct < 1 ? 2 : 1) + '%', `${fmt(a.missed)} de ${fmt(a.would_skip)} cortes auditados teriam perdido URL da janela`, ['bad', BAD + 'Há perda']);
    tiles.push(loss);
    tiles.push(tile('Leituras cortadas pelo topo', a.cut_pct === null ? '—' : dec(a.cut_pct, 0) + '%', 'documentos dispensados sem ler o resto'));
    tiles.push(tile('Sem mudança desde a última leitura', a.not_modified_pct === null ? '—' : dec(a.not_modified_pct, 0) + '%', 'resposta 304 ou conteúdo idêntico'));
    tiles.push(tile('Ordem de leitura contrariada', fmt(a.contradicted), 'links rebaixados por não seguirem a ordem comprovada', a.contradicted ? ['warn', WARN + 'Rebaixados'] : ['good', GOOD + 'Nenhum']));
    const cold = d.now.catalog.cold_links;
    tiles.push(tile('Links frios', fmt(cold), `lidos só na última rodada do dia (leitura adaptativa ${s.adaptive_polling === 'enforce' ? 'ligada' : 'desligada'}); ${fmt(a.cold_skipped)} pulados na última rodada`));
    $('audit').replaceChildren(...tiles);
  }

  /* ---------- carga ---------- */
  function render(d) {
    data = d;
    renderLast(d);
    renderNow(d);
    renderCharts(d);
    renderFailures(d);
    renderHealth(d);
    renderAudit(d);
    $('updated').textContent = `Atualizado às ${clock(d.generated_at)} · período de ${d.days} dias · atualiza sozinho a cada 30 s`;
  }

  async function load(initial = false) {
    if (busy) return;
    busy = true;
    $('panel').classList.toggle('is-refreshing', !initial);
    try {
      const response = await fetch('/api/gerenciador/busca/painel?days=' + days);
      let body;
      try { body = await response.json(); } catch { throw new Error('Não foi possível ler a resposta do painel. Tente de novo.'); }
      if (!response.ok) throw new Error(body.error || 'Não foi possível carregar o painel.');
      $('error').hidden = true;
      render(body);
    } catch (error) {
      $('error').textContent = error.message + ' Os números abaixo são os da última carga bem-sucedida.';
      $('error').hidden = false;
      if (initial) { $('last').replaceChildren(); $('charts').replaceChildren(); }
    } finally {
      busy = false;
      $('panel').classList.remove('is-refreshing');
    }
  }

  document.querySelectorAll('.segmented button').forEach((button) => {
    button.setAttribute('aria-pressed', String(Number(button.dataset.days) === days));
    button.onclick = () => {
      days = Number(button.dataset.days);
      try { localStorage.setItem('bm_panel_days', String(days)); } catch { /* ignora */ }
      document.querySelectorAll('.segmented button').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
      load();
    };
  });
  $('refresh').onclick = () => { const b = $('refresh'); UI.mark(b, true); load().finally(() => UI.mark(b, false)); };
  let resizeTimer = 0;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (data) { renderCharts.cards && drawAll(renderCharts.cards); renderHealth.card && renderHealth.card.draw(renderHealth.card.body); } }, 150);
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  UI.skeleton($('charts'), 4, 'card');
  load(true);
})();
