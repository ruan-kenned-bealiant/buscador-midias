'use strict';
/* Tela "Artigos": os registros do corpus com entidade, contexto e alerta, por site ou busca livre.
   Tudo o que vem do servidor entra na página como texto (nunca como HTML). */
const $ = (id) => document.getElementById(id);
const PAGE = 25;
const roles = { investigado: 'Investigado', outro: 'Outro papel' };
let offset = 0, total = 0, siteId = 0, latest = 0, siteTimer;
const sitesByLabel = new Map();

const fmt = (n) => new Intl.NumberFormat('pt-BR').format(n || 0);
const date = (value) => (value ? new Date(value).toLocaleString('pt-BR') : '—');
function el(tag, text, className) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = text; if (className) e.className = className; return e; }
function fail(text) { $('error').textContent = text; $('error').hidden = !text; }
async function api(path) {
  const r = await fetch('/api/gerenciador' + path);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Falha ao consultar o serviço.');
  return d;
}
/** Só endereços http(s) viram link; qualquer outro esquema (javascript:, data:) fica como texto. */
function safeUrl(value) {
  try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) ? u.href : null; } catch { return null; }
}
function link(text, href, className) {
  const safe = safeUrl(href);
  if (!safe) return el('span', text, className);
  const a = el('a', text, className); a.href = safe; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}

/* ---------- filtros ---------- */
function filters() {
  return { q: $('q').value.trim(), field: $('field').value, site_id: siteId || '', alert: $('alert').value, since: $('since').value, until: $('until').value, scope: $('scope').value };
}
function writeUrl(f) {
  const p = new URLSearchParams(Object.entries(f).filter(([k, v]) => v !== '' && !(k === 'field' && v === 'all') && !(k === 'scope' && v === 'searcher')));
  if (offset) p.set('offset', offset);
  history.replaceState(null, '', p.toString() ? '?' + p : location.pathname);
}
async function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const [key, id] of [['q', 'q'], ['field', 'field'], ['alert', 'alert'], ['since', 'since'], ['until', 'until'], ['scope', 'scope']]) if (p.get(key)) $(id).value = p.get(key);
  offset = Math.max(0, Number(p.get('offset')) || 0);
  const wanted = Number(p.get('site_id')) || 0;
  if (wanted) {
    try { const site = await api('/fontes/' + wanted); siteId = wanted; $('site').value = site.name; } catch { siteId = 0; }
  }
}

/* ---------- seletor de site ---------- */
async function suggestSites() {
  const text = $('site').value.trim();
  if (sitesByLabel.has(text)) { siteId = sitesByLabel.get(text); return; }
  siteId = 0;
  if (text.length < 2) { $('sites').replaceChildren(); return; }
  try {
    const d = await api('/fontes?' + new URLSearchParams({ q: text, limit: 15, status: 'all' }));
    $('sites').replaceChildren();
    for (const site of d.items) {
      const label = site.name + ' · ' + (site.identity || site.url);
      sitesByLabel.set(label, site.id);
      const option = document.createElement('option'); option.value = label; $('sites').append(option);
    }
  } catch (e) { fail(e.message); }
}
$('site').oninput = () => {
  const text = $('site').value.trim();
  if (!text) { siteId = 0; return; }
  clearTimeout(siteTimer); siteTimer = setTimeout(suggestSites, 250);
  if (sitesByLabel.has(text)) siteId = sitesByLabel.get(text);
};

/* ---------- resultados ---------- */
function analysisBlock(a) {
  const box = el('div', undefined, 'analysis');
  const head = el('div', undefined, 'meta');
  head.append(el('span', a.alert ? 'Alerta' : 'Sem alerta', 'badge ' + (a.alert ? 'amber' : 'gray')));
  if (a.themes.length) head.append(el('span', (a.themes.length === 1 ? 'Tema: ' : 'Temas: ') + a.themes.join(', ')));
  if (a.confidence !== null) head.append(el('span', 'Confiança ' + Math.round(a.confidence * 100) + '%'));
  if (a.role) head.append(el('span', 'Papel: ' + (roles[a.role] || a.role)));
  box.append(head);
  if (a.justification) box.append(el('p', a.justification));
  if (a.adverse_fact) { const p = el('p'); p.append(el('span', 'Fato adverso: ', 'label'), document.createTextNode(a.adverse_fact)); box.append(p); }
  return box;
}
function entityRow(e) {
  const row = el('div', undefined, 'entity'), who = el('div', undefined, 'who'), what = el('div', undefined, 'what');
  who.append(el('strong', e.name || '—'));
  const small = el('small');
  small.append(...[e.type, e.role && e.role !== 'outro' ? e.role : null, e.mentions ? fmt(e.mentions) + ' menç' + (e.mentions === 1 ? 'ão' : 'ões') : null].filter(Boolean).map((t, i, all) => document.createTextNode(t + (i < all.length - 1 ? ' · ' : ''))));
  who.append(small);
  if (e.alert) who.append(el('span', 'Alerta', 'badge amber'));
  if (e.context) { const p = el('p'); p.append(el('span', 'Contexto: ', 'label'), document.createTextNode(e.context)); what.append(p); }
  for (const a of e.analyses) what.append(analysisBlock(a));
  if (!e.context && !e.analyses.length) what.append(el('p', 'Sem contexto registrado para esta entidade.', 'muted'));
  row.append(who, what);
  return row;
}
function card(item) {
  const article = el('article', undefined, 'article');
  article.append(el('h3', item.title || '(sem título)'));
  article.append(link(item.url, item.url, 'link'));
  const meta = el('div', undefined, 'meta');
  meta.append(el('span', 'Entrada: ' + date(item.entered_at)), el('span', 'Publicado: ' + date(item.published_at)));
  if (item.source) meta.append(el('span', 'Site: ' + item.source));
  if (item.alert_count) meta.append(el('span', item.alert_count + ' alerta(s)', 'badge amber'));
  if (item.evidence_url) meta.append(link('Ver evidência', item.evidence_url));
  article.append(meta);
  const details = el('details');
  if (item.alert_count) details.open = true;
  details.append(el('summary', 'Entidades (' + fmt(item.entities_total) + ')'));
  for (const entity of item.entities) details.append(entityRow(entity));
  if (item.entities_total > item.entities.length) details.append(el('p', 'Mostrando as ' + item.entities.length + ' primeiras (alertas e mais citadas antes).', 'muted'));
  if (!item.entities_total) details.append(el('p', 'Nenhuma entidade registrada.', 'muted'));
  article.append(details);
  return article;
}
/** O último pedido vence: uma resposta atrasada de um filtro antigo nunca sobrescreve a tela. */
async function load() {
  const mine = ++latest;
  $('results').setAttribute('aria-busy', 'true');
  $('summary').textContent = 'Buscando…';
  const f = filters();
  try {
    const d = await api('/busca/artigos?' + new URLSearchParams({ ...Object.fromEntries(Object.entries(f).filter(([, v]) => v !== '')), offset, limit: PAGE }));
    if (mine !== latest) return;
    total = d.total; writeUrl(f);
    $('results').replaceChildren(...(d.items.length ? d.items.map(card) : [el('p', 'Nenhum artigo encontrado com estes filtros.', 'empty muted')]));
    $('summary').textContent = (d.capped ? 'Mais de ' : '') + fmt(d.total) + ' artigo(s)';
    $('page').textContent = total ? `${fmt(offset + 1)}–${fmt(Math.min(offset + PAGE, total))} de ${d.capped ? 'mais de ' : ''}${fmt(total)}` : '0 artigos';
    $('prev').disabled = offset === 0; $('next').disabled = offset + PAGE >= total;
    fail('');
  } catch (e) {
    if (mine !== latest) return;
    $('summary').textContent = ''; $('results').replaceChildren(); fail(e.message);
  } finally { if (mine === latest) $('results').removeAttribute('aria-busy'); }
}
$('filters').onsubmit = (event) => { event.preventDefault(); offset = 0; load(); };
$('clear').onclick = () => { $('filters').reset(); siteId = 0; offset = 0; load(); };
$('refresh').onclick = () => load();
$('prev').onclick = () => { offset = Math.max(0, offset - PAGE); load(); window.scrollTo({ top: 0 }); };
$('next').onclick = () => { offset += PAGE; load(); window.scrollTo({ top: 0 }); };
readUrl().then(load);
