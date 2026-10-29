'use strict';
/* Tela "Buscador de notícias": filas por etapa, sites da rodada e pendências.
   Cada notícia é uma linha só; a etapa, o resultado e o motivo da falha vêm nela. */
const $ = (id) => document.getElementById(id);
const stages = { discovery: 'Descoberta', dedup: 'Verificação no corpus', fetch: 'Extração textual', context: 'Análise de contexto', evidence: 'Evidência', persist: 'Gravação no corpus' };
const states = { pending: 'Aguardando', queued: 'Na fila', running: 'Processando', retry: 'Nova tentativa', succeeded: 'Concluída', failed: 'Falha', cancelled: 'Cancelada', completed: 'Concluída', partial: 'Concluída com pendências' };
const outcomes = {
  dedup: 'Aguardando verificação no corpus', fetch: 'Aguardando extração', context: 'Aguardando análise', evidence: 'Aguardando evidência', persist: 'Aguardando gravação',
  known: 'Já existe no corpus', no_text: 'Sem texto confirmado', outside_window: 'Fora da janela', date_unknown: 'Data desconhecida', no_entities: 'Sem entidade identificável',
  excluded_content: 'Conteúdo rotineiro descartado', persisted: 'Gravada com evidência', evidence_pending: 'Gravada com evidência pendente', failed: 'Falha de processamento', cancelled: 'Cancelada',
};
const classes = { transient: 'Transitória', blocked: 'Bloqueio do site', permanent: 'Permanente', bug: 'Falha interna' };
let token = '', version = 0, config = null, offset = 0, total = 0, busy = false, timer;
let currentView = 'stages', sourceOffset = 0, sourceTotal = 0, sourceRun = '', searchTimer;
let pendingOffset = 0, pendingTotal = 0;

const fmt = (n) => new Intl.NumberFormat('pt-BR').format(n || 0);
const date = (value) => (value ? new Date(value).toLocaleString('pt-BR') : '—');
function el(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
function notify(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(timer); timer = setTimeout(() => { $('toast').hidden = true; }, 5000); }
function fail(text) { $('error').textContent = text; $('error').hidden = !text; }
async function api(path, method = 'GET', data) {
  const r = await fetch('/api/gerenciador' + path, { method, headers: { 'Content-Type': 'application/json', ...(method !== 'GET' ? { 'X-Catalog-Token': token } : {}) }, body: data === undefined ? undefined : JSON.stringify(data) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Falha ao consultar o serviço.');
  return d;
}
function badge(state) { return el('span', states[state] || state, 'badge ' + (['succeeded', 'completed'].includes(state) ? 'green' : ['failed', 'partial', 'retry'].includes(state) ? 'amber' : '')); }
function button(text, handler) { const b = el('button', text); b.type = 'button'; b.onclick = handler; return b; }
function emptyRow(id, text, span) { const row = el('tr'), cell = el('td', text, 'empty'); cell.colSpan = span; row.append(cell); $(id).append(row); }
function tableSkeleton(id, rows = 4) {
  const nodes = [];
  for (let i = 0; i < rows; i++) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 8; td.className = 'skeleton-cell'; td.setAttribute('aria-hidden', 'true'); const line = document.createElement('span'); line.className = 'skeleton line'; td.append(line); tr.append(td); nodes.push(tr); }
  $(id).replaceChildren(...nodes);
}
/** Botão de duas etapas (sem janela de confirmação, que o navegador do painel não mostra). */
function confirming(text, confirmText, action) {
  const b = el('button', text); b.type = 'button';
  let armed = null;
  b.onclick = async () => {
    if (!armed) { b.textContent = confirmText; armed = setTimeout(() => { armed = null; b.textContent = text; }, 6000); return; }
    clearTimeout(armed); armed = null; b.disabled = true;
    try { await action(); } catch (e) { fail(e.message); } finally { b.disabled = false; b.textContent = text; }
  };
  return b;
}

/** Custos por etapa: consulta pesada, por isso vem depois da tela e só é refeita a cada 60 s. */
let costsKey = '', costsAt = 0, costsSerial = 0;
function costsText(metrics) {
  return metrics.map((m) => (stages[m.stage] || m.stage) + ': ' + fmt(m.bright_data_requests) + ' chamadas Bright Data · ' + fmt((Number(m.avoided_article_requests) || 0) + (Number(m.avoided_xml_requests) || 0)) + ' leituras evitadas · ' + fmt(m.prompt_tokens) + ' tokens de entrada · ' + (m.cost_usd == null ? 'custo OpenRouter não informado' : 'US$ ' + Number(m.cost_usd).toFixed(4))).join(' | ');
}
async function loadCosts(runId) {
  const source = $('source-filter').value, key = runId + '|' + source;
  if (key === costsKey && Date.now() - costsAt < 60000) return;
  if (key !== costsKey) $('costs').textContent = 'Calculando custos…';
  costsKey = key; costsAt = Date.now();
  const serial = ++costsSerial;
  try { const d = await api('/busca/custos?' + new URLSearchParams({ run_id: runId, source_id: source })); if (serial === costsSerial) $('costs').textContent = costsText(d.metrics); }
  catch (e) { if (serial === costsSerial) { costsKey = ''; $('costs').textContent = 'Não foi possível calcular os custos agora.'; } }
}

/* ---------- configuração ---------- */
function renderSettings(setting) {
  config = setting.config; version = setting.version;
  for (const [key, value] of Object.entries(config)) {
    if (key === 'workers') continue;
    const input = $('settings').elements.namedItem(key);
    if (input) { if (input.type === 'checkbox') input.checked = value; else input.value = Array.isArray(value) ? value.join(', ') : value; }
  }
  $('worker-fields').replaceChildren();
  for (const [stage, title] of Object.entries(stages)) {
    const label = el('label', undefined, 'field'), input = el('input');
    input.type = 'number'; input.min = 1; input.max = 64; input.required = true; input.name = 'worker_' + stage; input.value = config.workers[stage];
    label.append(el('span', title), input); $('worker-fields').append(label);
  }
  $('setting-version').textContent = 'Revisão ' + version;
}

/* ---------- filas por etapa ---------- */
function brokerOf(data, stage) {
  const rows = (data.broker?.queues || []).filter((q) => q.stage === stage);
  if (!rows.length || rows.every((q) => q.messages === null)) return null;
  return { messages: rows.reduce((n, q) => n + (q.messages || 0), 0), consumers: rows.reduce((n, q) => n + (q.consumers || 0), 0) };
}
function drawQueues(data) {
  $('queues').replaceChildren();
  for (const [stage, title] of Object.entries(stages)) {
    const counts = {}; let oldest;
    for (const r of data.queues.filter((q) => q.stage === stage)) {
      counts[r.state] = Number(r.n);
      if (['queued', 'pending', 'retry'].includes(r.state) && (!oldest || r.oldest < oldest)) oldest = r.oldest;
    }
    const card = el('article', undefined, 'queue');
    card.append(el('h3', title), el('strong', fmt((counts.pending || 0) + (counts.queued || 0))), el('small', 'aguardando'));
    const pairs = el('div', undefined, 'pairs');
    for (const [key, label] of [['running', 'Processando'], ['retry', 'Nova tentativa'], ['succeeded', 'Concluídas'], ['failed', 'Falhas']]) pairs.append(el('span', label), el('span', fmt(counts[key])));
    card.append(pairs);
    const live = brokerOf(data, stage);
    if (live) {
      card.append(el('small', 'Fila do RabbitMQ: ' + fmt(live.messages) + ' · Workers: ' + fmt(live.consumers)));
      if (!live.consumers && ((counts.pending || 0) + (counts.queued || 0) + (counts.retry || 0))) card.append(el('small', 'Sem worker consumindo esta etapa.', 'badge amber'));
    }
    if (oldest) card.append(el('small', 'Mais antigo: ' + date(oldest)));
    card.append(button('Ver fila', () => { $('stage-filter').value = stage; offset = 0; load(); }));
    $('queues').append(card);
  }
}
function serviceText(d) {
  const enabled = d.settings.config.enabled ? 'Agendamento habilitado.' : 'Novos agendamentos pausados.';
  const workers = d.settings.config.workers_enabled ? ' Workers habilitados.' : ' Workers pausados.';
  if (!d.broker?.ok) return 'RabbitMQ indisponível: não é possível ver as filas nem os workers agora. ' + enabled + workers;
  const online = Object.entries(stages).map(([stage, title]) => [title, brokerOf(d, stage)?.consumers || 0]);
  if (online.every(([, n]) => !n)) return 'RabbitMQ conectado, mas nenhum worker está consumindo. Inicie os serviços do buscador. ' + enabled + workers;
  return 'RabbitMQ conectado. Workers por etapa: ' + online.map(([title, n]) => title + ' ' + n).join(' · ') + '. ' + enabled + workers;
}

/* ---------- rodadas e notícias ---------- */
function drawRuns(d, initial) {
  $('runs').replaceChildren();
  const selected = $('run-filter').value || (initial ? d.run_id : '');
  $('run-filter').replaceChildren(new Option('Todas as rodadas', ''));
  for (const r of d.runs) {
    $('run-filter').add(new Option(date(r.created_at) + ' · ' + r.id.slice(0, 8), r.id));
    const row = el('tr'), first = el('td');
    first.append(el('strong', date(r.created_at)), el('p', date(r.since) + ' → ' + date(r.until)));
    const status = el('td'); status.append(badge(r.state));
    if (r.state === 'running') status.append(el('p', (Number(r.primary_pending) === 0 ? 'Recuperação em andamento' : 'Principal: ' + fmt(r.primary_pending) + ' pendentes') + ' · Recuperação: ' + fmt(r.recovery_pending), 'muted'));
    const action = el('td'); action.append(button('Ver rodada', () => { $('run-filter').value = r.id; offset = 0; load(); }));
    row.append(first, status, el('td', fmt(r.links_done) + ' / ' + fmt(r.links)), el('td', fmt(r.occurrences) + ' / ' + fmt(r.unique_articles)), action);
    $('runs').append(row);
  }
  if (selected && !Array.from($('run-filter').options).some((o) => o.value === selected)) $('run-filter').add(new Option(selected, selected));
  $('run-filter').value = selected;
  if (!d.runs.length) emptyRow('runs', 'Nenhuma rodada ainda. Configure a janela e execute um primeiro ciclo.', 5);
}
function reasonOf(item) {
  if (item.state === 'failed') return (item.reason || 'Motivo não registrado') + (item.reason_class ? ' · ' + classes[item.reason_class] : '');
  return outcomes[item.outcome] || item.outcome || '';
}
async function reprocess(item) {
  const path = item.kind === 'article' ? '/busca/noticias/' + item.id + '/reprocessar' : null;
  if (!path) return;
  await api(path, 'POST', {});
  notify('Notícia reenfileirada na etapa em que falhou.');
}
function drawItems(d) {
  $('jobs').replaceChildren();
  for (const item of d.items) {
    const row = el('tr'), url = el('td', undefined, 'url'), state = el('td'), actions = el('td');
    if (item.title) url.append(el('strong', item.title), el('br'));
    url.append(document.createTextNode(item.url));
    state.append(badge(item.state), el('p', reasonOf(item)));
    if (item.lane === 'recovery') state.append(el('p', 'Fila de recuperação', 'muted'));
    actions.append(button('Detalhes', () => showDetail(item)));
    if (item.state === 'failed' && item.kind === 'article') actions.append(button('Reprocessar', async (e) => { e.target.disabled = true; try { await reprocess(item); await load(); } catch (error) { fail(error.message); } finally { e.target.disabled = false; } }));
    row.append(url, el('td', item.source + (item.link_id ? ' · #' + item.link_id : '')), el('td', stages[item.stage] || item.stage), state, el('td', fmt(item.attempts)), actions);
    $('jobs').append(row);
  }
  if (!d.items.length) emptyRow('jobs', 'Nada neste filtro.', 6);
  total = d.total;
  $('page').textContent = total ? `${offset + 1}–${Math.min(offset + 25, total)} de ${fmt(total)}` : '0 itens';
  $('prev').disabled = offset === 0; $('next').disabled = offset + 25 >= total;
}
async function showDetail(item) {
  $('detail-title').textContent = (stages[item.stage] || item.stage) + ' · ' + (states[item.state] || item.state);
  const body = $('detail-body'); body.replaceChildren();
  const info = el('article');
  info.append(el('p', item.source + (item.link_id ? ' · Link #' + item.link_id : '')), el('p', item.url), el('p', 'Criada: ' + date(item.created_at) + ' · Última atividade: ' + date(item.updated_at)), el('p', 'Resultado: ' + (reasonOf(item) || 'Aguardando')));
  if (item.detail) info.append(el('p', item.detail));
  body.append(info);
  for (const entry of item.history || []) { const row = el('article'); row.append(el('strong', (stages[entry.stage] || entry.stage || '') + ' · tentativa ' + (entry.attempt ?? '')), el('p', JSON.stringify(entry))); body.append(row); }
  if (!$('detail').open) $('detail').showModal();
  if (item.kind !== 'article') return;
  try {
    const d = await api('/busca/noticias/' + item.id), a = d.article, p = a.payload || {}, section = el('article');
    section.append(el('h3', p.title || 'Notícia'), el('p', (outcomes[a.outcome] || outcomes[a.stage] || a.outcome || '') + ' · ' + (states[a.state] || a.state)));
    if (/^https?:\/\//i.test(a.url)) { const link = el('a', a.url); link.href = a.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; section.append(link); }
    section.append(el('p', 'Publicação: ' + date(p.published_at)), el('p', 'Artigo no corpus: ' + (a.corpus_article_id || 'ainda não gravado')), el('p', 'Origem: ' + d.origin.source + ' · visto ' + fmt(d.origin.seen_count) + ' vez(es) · primeira vez em ' + date(d.origin.first_seen_at)));
    if (d.origin.document_url) section.append(el('p', 'Documento: ' + d.origin.document_url));
    const cost = el('details'); cost.append(el('summary', 'Consumo por etapa'), el('pre', JSON.stringify(a.cost, null, 2))); section.append(cost);
    body.append(section);
  } catch (e) { body.append(el('p', e.message, 'error')); }
}
async function load(initial = false) {
  if (busy) return;
  busy = true;
  try {
    const params = new URLSearchParams({ offset, stage: $('stage-filter').value, state: $('state-filter').value, run_id: initial && !$('run-filter').value ? 'latest' : $('run-filter').value, source_id: $('source-filter').value });
    const d = await api('/busca?' + params);
    if (initial) renderSettings(d.settings);
    const c = d.settings.config;
    $('schedule-caption').textContent = 'Horários configurados: ' + c.schedule_hours.map((h) => String(h).padStart(2, '0') + 'h').join(', ') + ' · ' + (c.timezone === 'America/Sao_Paulo' ? 'Brasília' : 'UTC') + '. Cada rodada começa no horário marcado, não por intervalo de link.';
    drawQueues(d);
    $('service').textContent = serviceText(d);
    drawRuns(d, initial);
    $('funnel').replaceChildren(...d.funnel.map((r) => el('span', (outcomes[r.status] || r.status) + ': ' + fmt(r.n))));
    loadCosts(d.run_id || $('run-filter').value);
    drawItems(d);
    $('updated').textContent = 'Atualizado ' + new Date().toLocaleTimeString('pt-BR');
    if (currentView === 'sources') await loadSources();
    if (currentView === 'pending') await loadPending();
    fail('');
  } catch (e) { fail(e.message); } finally { busy = false; }
}

/* ---------- formulários ---------- */
$('settings').onsubmit = async (event) => {
  event.preventDefault();
  const submit = $('settings').querySelector('[type=submit]'); UI.mark(submit, true);
  try {
    const patch = {};
    for (const key of Object.keys(config)) {
      if (key === 'workers') { patch.workers = Object.fromEntries(Object.keys(stages).map((s) => [s, Number($('settings').elements.namedItem('worker_' + s).value)])); continue; }
      const input = $('settings').elements.namedItem(key);
      if (!input) continue;
      patch[key] = key === 'schedule_hours' ? input.value.split(',').map((s) => Number(s.trim())) : key === 'blocked_content_categories' ? input.value.split(',').map((s) => s.trim()).filter(Boolean) : input.type === 'checkbox' ? input.checked : (input.type === 'number' || key === 'lookback_minutes') ? Number(input.value) : input.value;
    }
    const d = await api('/busca/configuracao', 'PATCH', { version, config: patch });
    renderSettings(d); notify('Configuração salva para as próximas rodadas.'); await load();
  } catch (e) { fail(e.message); } finally { UI.mark(submit, false); }
};
$('manual').onsubmit = async (event) => {
  event.preventDefault();
  const submit = $('manual').querySelector('[type=submit]'); UI.mark(submit, true);
  try {
    const data = {};
    if ($('day').value) data.day = $('day').value;
    if ($('link-ids').value.trim()) data.link_ids = $('link-ids').value.split(',').map((v) => Number(v.trim()));
    const r = await api('/busca/rodadas', 'POST', data);
    notify('Rodada criada com ' + fmt(r.links) + ' links.'); await load(); $('run-filter').value = r.id; await load();
  } catch (e) { fail(e.message); } finally { UI.mark(submit, false); }
};
for (const [stage, title] of Object.entries(stages)) { $('stage-filter').add(new Option(title, stage)); }
for (const id of ['run-filter', 'stage-filter', 'state-filter', 'source-filter']) {
  $(id).onchange = () => { offset = 0; if (id === 'run-filter') { sourceOffset = 0; pendingOffset = 0; $('source-filter').value = '0'; } load(); };
}
$('refresh').onclick = () => { UI.mark($('refresh'), true); load().finally(() => UI.mark($('refresh'), false)); };
$('prev').onclick = () => { offset = Math.max(0, offset - 25); load(); };
$('next').onclick = () => { offset += 25; load(); };
$('close-detail').onclick = () => $('detail').close();

/* ---------- visões ---------- */
function chooseSource(source, stage = '') {
  if (!Array.from($('source-filter').options).some((o) => o.value === String(source.id))) $('source-filter').add(new Option(source.name, source.id));
  $('source-filter').value = source.id; $('stage-filter').value = stage; $('state-filter').value = '';
  if (sourceRun) $('run-filter').value = sourceRun;
  offset = 0; setView('stages');
  $('jobs').closest('section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function choosePendingSource(source) {
  if (!Array.from($('pending-source').options).some((o) => o.value === String(source.id))) $('pending-source').add(new Option(source.name, source.id));
  $('pending-source').value = source.id; pendingOffset = 0; setView('pending');
}
const captions = {
  stages: 'Visão geral das filas e notícias. O filtro de site restringe as etapas ao site selecionado.',
  sources: 'Acompanhamento de cada site na rodada selecionada. Sem seleção, usa a rodada mais recente.',
  pending: 'Notícias que falharam. Reabra por grupo, por site ou uma a uma.',
};
function setView(view) {
  currentView = view;
  $('source-view').hidden = view !== 'sources'; $('stage-view').hidden = view !== 'stages'; $('pending-view').hidden = view !== 'pending';
  for (const name of ['stages', 'sources', 'pending']) $('view-' + name).setAttribute('aria-selected', String(view === name));
  $('view-caption').textContent = captions[view];
  load();
}
async function loadSources() {
  const params = new URLSearchParams({ run_id: $('run-filter').value, offset: sourceOffset, search: $('source-search').value });
  const data = await api('/busca/fontes?' + params);
  sourceTotal = data.total; sourceRun = data.run?.id || '';
  const expanded = new Set(Array.from($('source-cards').querySelectorAll('details[open]')).map((d) => d.dataset.source));
  $('source-cards').replaceChildren();
  $('source-summary').textContent = data.run ? date(data.run.created_at) + ' · ' + fmt(data.summary.running) + ' em andamento · ' + fmt(data.summary.completed) + ' concluídos · ' + fmt(data.summary.partial) + ' com pendências' : 'Nenhuma rodada disponível.';
  for (const source of data.sources) $('source-cards').append(sourceCard(source, expanded));
  if (!data.sources.length) $('source-cards').append(el('p', 'Nenhum site encontrado nesta rodada.', 'empty'));
  $('source-page').textContent = sourceTotal ? `${sourceOffset + 1}–${Math.min(sourceOffset + 25, sourceTotal)} de ${fmt(sourceTotal)} sites` : '0 sites';
  $('source-prev').disabled = sourceOffset === 0; $('source-next').disabled = sourceOffset + 25 >= sourceTotal;
}
function sourceCard(source, expanded) {
  const card = el('article', undefined, 'source-card'), header = el('div', undefined, 'flex'), title = el('div');
  title.append(el('h3', source.name), el('p', fmt(source.links_done) + ' / ' + fmt(source.links) + ' links lidos · ' + fmt(source.articles) + ' URLs únicas', 'muted'));
  header.append(title, badge(source.state)); card.append(header);
  const progress = el('progress', undefined, source.state === 'partial' ? 'partial' : ''); progress.max = 100; progress.setAttribute('aria-label', 'Progresso de ' + source.name);
  if (source.progress !== null) progress.value = source.progress;
  card.append(progress, el('p', source.progress === null ? 'Descobrindo o total de notícias…' : source.progress + '% das etapas resolvidas' + (source.state === 'partial' ? ' · encerrado com pendências' : '')));
  const statuses = el('div', undefined, 'source-status');
  for (const [status, n] of Object.entries(source.statuses)) statuses.append(el('span', (outcomes[status] || status) + ': ' + fmt(n)));
  card.append(statuses);
  const detail = el('details'); detail.dataset.source = String(source.id); detail.open = expanded.has(String(source.id)); detail.append(el('summary', 'Detalhar etapas'));
  const wrap = el('div', undefined, 'table-wrap'), table = el('table', undefined, 'source-table'), head = el('thead'), hr = el('tr');
  for (const text of ['Etapa', 'Aguardando', 'Processando', 'Retentativa', 'Concluídas', 'Falhas', 'Canceladas', '']) hr.append(el('th', text));
  head.append(hr); table.append(head);
  const tbody = el('tbody');
  for (const [stage, label] of Object.entries(stages)) {
    const counts = {};
    for (const row of source.stages.filter((r) => r.stage === stage)) counts[row.state] = Number(row.n);
    const tr = el('tr'); tr.append(el('td', label));
    for (const n of [(counts.pending || 0) + (counts.queued || 0), counts.running, counts.retry, counts.succeeded, counts.failed, counts.cancelled]) tr.append(el('td', fmt(n)));
    const action = el('td'); action.append(button('Ver itens', () => chooseSource(source, stage))); tr.append(action); tbody.append(tr);
  }
  table.append(tbody); wrap.append(table); detail.append(wrap); card.append(detail);
  const actions = el('div', undefined, 'actions'), run = sourceRun;
  actions.append(button('Ver notícias no corpus', () => showSourceCorpus(source, run)), button('Ver todos os itens deste site', () => chooseSource(source)), button('Ver pendências do site', () => choosePendingSource(source)), button('Ver artigos do site', () => { location.href = '/artigos?site_id=' + source.id; }));
  card.append(actions);
  return card;
}
async function showSourceCorpus(source, run, from = 0) {
  $('detail-title').textContent = source.name + ' · notícias no corpus';
  const body = $('detail-body'); body.replaceChildren(el('p', 'Carregando notícias…', 'muted'));
  if (!$('detail').open) $('detail').showModal();
  try {
    const data = await api('/busca/fontes/' + source.id + '/corpus?' + new URLSearchParams({ run_id: run, offset: from }));
    body.replaceChildren(el('p', fmt(data.total) + ' notícia(s) registrada(s) no corpus e encontrada(s) na rodada selecionada.', 'muted'));
    for (const article of data.articles) {
      const section = el('article'); section.append(el('h3', article.title || 'Notícia sem título'), el('p', 'Publicação: ' + date(article.published_at)));
      if (/^https?:\/\//i.test(article.url)) { const original = el('a', 'Abrir notícia no site'); original.href = article.url; original.target = '_blank'; original.rel = 'noopener noreferrer'; section.append(original); }
      const actions = el('div', undefined, 'actions');
      if (article.evidence_url) { const evidence = el('a', 'Abrir evidência'); evidence.href = article.evidence_url; evidence.target = '_blank'; evidence.rel = 'noopener noreferrer'; actions.append(evidence); } else actions.append(el('span', 'Evidência ainda não disponível.', 'muted'));
      section.append(actions); body.append(section);
    }
    if (!data.articles.length) body.append(el('p', 'Nenhuma notícia deste site está registrada no corpus para esta rodada.', 'empty'));
    const pagination = el('div', undefined, 'pager'), previous = button('Anterior', () => showSourceCorpus(source, run, Math.max(0, from - 25))), next = button('Próxima', () => showSourceCorpus(source, run, from + 25));
    previous.disabled = from === 0; next.disabled = from + 25 >= data.total;
    pagination.append(previous, el('span', data.total ? `${from + 1}–${Math.min(from + 25, data.total)} de ${fmt(data.total)}` : '0 notícias'), next); body.append(pagination);
  } catch (error) { body.replaceChildren(el('p', error.message, 'error')); }
}

/* ---------- pendências ---------- */
function pendingParams(extra = {}) {
  const p = { offset: pendingOffset, stage: $('pending-stage').value, reason_class: $('pending-class').value, source_id: $('pending-source').value, run_id: $('run-filter').value, ...extra };
  return new URLSearchParams(Object.fromEntries(Object.entries(p).filter(([, v]) => v !== '' && v !== '0' && v !== 0)));
}
async function requeue(filter) {
  const d = await api('/busca/pendencias/reprocessar', 'POST', filter);
  notify(fmt(d.requeued) + ' notícia(s) reenfileirada(s). O andamento aparece em "Pedidos de reprocessamento".');
  await load();
}
/* ---------- pedidos de reprocessamento ---------- */
function requestScope(r) {
  const f = r.filters || {}, parts = [];
  if (f.everything) parts.push('todas as pendências');
  if (f.hashes) parts.push(fmt(f.hashes) + ' notícia(s) escolhida(s)');
  if (f.stage) parts.push('etapa: ' + (stages[f.stage] || f.stage));
  if (f.reason) parts.push('motivo: ' + f.reason);
  if (f.reason_class) parts.push((classes[f.reason_class] || f.reason_class).toLowerCase());
  if (f.source_id) parts.push('site: ' + (r.source_name || '#' + f.source_id));
  if (f.run_id) parts.push('rodada ' + String(f.run_id).slice(0, 8));
  return parts.join(' · ') || 'todas as pendências';
}
function requestState(r) {
  if (!r.finished) return 'Em andamento';
  return r.failed ? 'Terminou com ' + fmt(r.failed) + ' falha(s)' : 'Terminou sem falhas';
}
function drawRetryRequests(requests) {
  $('retry-requests').replaceChildren();
  for (const r of requests) {
    const row = el('tr'), scope = el('td'), situation = el('td'), resolved = r.persisted + r.done_other + r.failed + r.cancelled;
    scope.append(el('span', requestScope(r)));
    if (r.reasons.length) scope.append(el('br'), el('small', 'Falhas: ' + r.reasons.map((x) => (x.reason || 'motivo não registrado') + ' (' + fmt(x.n) + ')').join(', '), 'muted'));
    if (r.superseded) scope.append(el('br'), el('small', fmt(r.superseded) + ' reaberta(s) de novo por um pedido mais novo.', 'muted'));
    const progress = el('progress'); progress.max = Math.max(r.requested - r.superseded, 1); progress.value = resolved; progress.setAttribute('aria-label', 'Andamento do pedido');
    situation.append(el('span', requestState(r), 'badge ' + (!r.finished ? '' : r.failed ? 'amber' : 'green')), progress);
    row.append(el('td', date(r.created_at)), scope, el('td', fmt(r.requested)), el('td', fmt(r.in_progress)), el('td', fmt(r.persisted)), el('td', fmt(r.done_other)), el('td', fmt(r.failed)), situation);
    $('retry-requests').append(row);
  }
  if (!requests.length) emptyRow('retry-requests', 'Nenhum reprocessamento pedido ainda.', 8);
}
async function loadPending() {
  const d = await api('/busca/pendencias?' + pendingParams());
  pendingTotal = d.total;
  drawRetryRequests(d.retries || []);
  $('pending-summary').textContent = fmt(d.total) + ' notícia(s) com pendência neste filtro.';
  $('pending-groups').replaceChildren(); $('pending-items').replaceChildren();
  for (const g of d.groups) {
    const row = el('tr'), action = el('td');
    const filter = { stage: g.failed_stage, reason: g.reason, ...(g.reason_class ? { reason_class: g.reason_class } : {}) };
    action.append(confirming('Reprocessar grupo', 'Confirmar: ' + fmt(g.n) + ' notícia(s)', () => requeue(filter)));
    row.append(el('td', stages[g.failed_stage] || g.failed_stage), el('td', g.reason || 'Motivo não registrado'), el('td', classes[g.reason_class] || '—'), el('td', fmt(g.n)), el('td', date(g.last_at)), action);
    $('pending-groups').append(row);
  }
  if (!d.groups.length) emptyRow('pending-groups', 'Nenhuma pendência neste filtro.', 6);
  const transient = d.groups.filter((g) => g.reason_class === 'transient').reduce((n, g) => n + g.n, 0);
  if (transient) { const row = el('tr'), cell = el('td'); cell.colSpan = 6; cell.append(confirming('Reprocessar todas as transitórias', 'Confirmar: ' + fmt(transient) + ' notícia(s)', () => requeue({ reason_class: 'transient' }))); row.append(cell); $('pending-groups').append(row); }
  for (const item of d.items) {
    const row = el('tr'), url = el('td', undefined, 'url'), action = el('td');
    if (item.title) url.append(el('strong', item.title), el('br'));
    url.append(document.createTextNode(item.url));
    action.append(button('Detalhes', () => showDetail(item)), button('Reprocessar', async (e) => { e.target.disabled = true; try { await reprocess(item); await load(); } catch (error) { fail(error.message); } finally { e.target.disabled = false; } }));
    row.append(url, el('td', item.source), el('td', stages[item.failed_stage] || item.failed_stage), el('td', (item.reason || '—') + (item.reason_class ? ' · ' + classes[item.reason_class] : '')), el('td', fmt(item.passes)), action);
    $('pending-items').append(row);
  }
  if (!d.items.length) emptyRow('pending-items', 'Nenhuma notícia com pendência.', 6);
  $('pending-page').textContent = pendingTotal ? `${pendingOffset + 1}–${Math.min(pendingOffset + 25, pendingTotal)} de ${fmt(pendingTotal)}` : '0 notícias';
  $('pending-prev').disabled = pendingOffset === 0; $('pending-next').disabled = pendingOffset + 25 >= pendingTotal;
}
for (const [stage, title] of Object.entries(stages)) { if (stage !== 'discovery') $('pending-stage').add(new Option(title, stage)); }
for (const id of ['pending-stage', 'pending-class', 'pending-source']) $(id).onchange = () => { pendingOffset = 0; loadPending().catch((e) => fail(e.message)); };
$('pending-prev').onclick = () => { pendingOffset = Math.max(0, pendingOffset - 25); loadPending(); };
$('pending-next').onclick = () => { pendingOffset += 25; loadPending(); };
$('view-stages').onclick = () => setView('stages'); $('view-sources').onclick = () => setView('sources'); $('view-pending').onclick = () => setView('pending');
$('source-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { sourceOffset = 0; load(); }, 300); };
$('source-prev').onclick = () => { sourceOffset = Math.max(0, sourceOffset - 25); load(); };
$('source-next').onclick = () => { sourceOffset += 25; load(); };

/* ---------- início ---------- */
UI.skeleton($('queues'), 6, 'card');
for (const id of ['runs', 'jobs']) tableSkeleton(id);
api('/resumo').then((d) => { token = d.csrf_token; return load(true); }).catch((e) => { $('queues').replaceChildren(); for (const id of ['runs', 'jobs']) $(id).replaceChildren(); fail(e.message); });
setInterval(() => { if (!document.hidden && !$('detail').open && !document.activeElement.closest('form')) load(); }, 5000);
