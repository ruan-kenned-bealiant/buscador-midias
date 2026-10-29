'use strict';
let healthOffset=0,healthTotal=0,healthRun=null,healthDetailOffset=0,healthDetailSerial=0,healthBusy=false;
const healthNames={active:'Links ativos',recovery:'Inativos com falhas de acesso'};
const healthLabels={queued:'Na fila',running:'Em execução',cancelling:'Cancelando',completed:'Concluída',cancelled:'Cancelada',pending:'Pendente',healthy:'Notícia textual confirmada',access_error:'Falha de acesso',no_news:'Acessível, sem notícias na amostra',no_text:'Sem notícia textual confirmada',invalid:'Formato ou mecanismo inválido',inconclusive:'Inconclusivo',error:'Falha interna',skipped:'Cadastro alterado'};
function healthRunName(run){if(run.configuration.scope==='auto_excluded_access')return 'Recuperação de exclusões por falha de acesso';return run.kind==='recovery'&&run.configuration.scope==='inactive'?'Varredura de todos os inativos':healthNames[run.kind]}
function healthDate(value){return value?new Date(value).toLocaleString('pt-BR'):'—'}
function healthBadge(value){return el('span',healthLabels[value]||value,'badge '+(value==='healthy'||value==='completed'?'green':value==='access_error'||value==='invalid'||value==='error'?'amber':'blue'))}
function healthSettings(setting){
 const card=el('form',undefined,'panel health-setting');card.dataset.kind=setting.kind;card.dataset.version=setting.version;
 card.append(el('h2',healthNames[setting.kind]));
 card.append(el('p',setting.kind==='active'?'Confere todos os links ativos em sites ativos.':'Retesta cadastros inativos cuja última verificação comprovou falha de acesso, rede ou permissão.','muted'));
 const fields=el('div',undefined,'health-fields');
 for(const [key,title,min,max] of [['interval_minutes','Repetir a cada (minutos)',1,525600],['workers','Sites em paralelo',1,32],['max_attempts','Tentativas por link',1,3],['max_bright_requests','Chamadas Bright Data por teste',1,48]]){
  const label=el('label',undefined,'field'),input=el('input');input.name=key;input.type='number';input.required=true;input.min=min;input.max=max;input.step=1;input.value=setting[key];label.append(el('span',title),input);fields.append(label);
 }
 card.append(fields);
 for(const [key,title]of [['bright_data','Usar Bright Data após falha no acesso direto (chamadas pagas)'],...(setting.kind==='recovery'?[['activate_recovered','Ativar os links recuperados com notícia textual confirmada']]:[])]){const box=el('label',undefined,'check'),input=el('input');input.type='checkbox';input.name=key;input.checked=!!setting[key];box.append(input,document.createTextNode(' '+title));card.append(box)}
 if(setting.kind==='recovery'){const label=el('label',undefined,'field'),scope=el('select');scope.name='manual_scope';scope.add(new Option('Somente inativos com falhas de acesso','errors'));scope.add(new Option('Todos os inativos visíveis','inactive'));scope.add(new Option('Excluídos automaticamente por falha de acesso','auto_excluded_access'));label.append(el('span','Escopo da execução manual'),scope);card.append(label)}
 const label=el('label',undefined,'check'),enabled=el('input');enabled.type='checkbox';enabled.name='enabled';enabled.checked=setting.enabled;label.append(enabled,document.createTextNode(' Execução periódica habilitada'));card.append(label);
 const next=el('p',setting.enabled?'Próxima execução: '+healthDate(setting.next_run_at):'Agendamento pausado. Você ainda pode executar manualmente.','muted');next.dataset.next=setting.kind;card.append(next);
 const buttons=el('div',undefined,'actions'),save=el('button','Salvar configuração','primary');save.type='submit';buttons.append(save,action('Executar agora',async b=>{UI.mark(b,true);try{const run=await api('/saude/rodadas','POST',{kind:setting.kind,...(setting.kind==='recovery'?{scope:card.elements.manual_scope.value}:{})});notify('Rodada adicionada à fila.');await loadHealth();await openHealthRun(run.id)}catch(e){alertIn('health-error',e.message)}finally{UI.mark(b,false)}}));card.append(buttons);
 card.onsubmit=async event=>{event.preventDefault();UI.mark(save,true);try{const data={version:Number(card.dataset.version),enabled:enabled.checked};for(const key of ['interval_minutes','workers','max_attempts','max_bright_requests'])data[key]=Number(card.elements.namedItem(key).value);data.bright_data=card.elements.bright_data.checked;data.activate_recovered=card.elements.activate_recovered?.checked||false;const updated=await api('/saude/configuracoes/'+setting.kind,'PATCH',data);card.replaceWith(healthSettings(updated));alertIn('health-error','');notify('Rotina atualizada. A configuração das buscas de notícias permanece independente.')}catch(e){alertIn('health-error',e.message)}finally{UI.mark(save,false)}};
 return card;
}
async function loadHealth(initial=false){
 if(healthBusy)return;healthBusy=true;if(!$('health-runs').children.length)UI.skeleton($('health-runs'),3,'row');
 try{
  const d=await api('/saude?offset='+healthOffset);healthTotal=d.total;
  if(initial||!$('health-settings').children.length)$('health-settings').replaceChildren(...d.settings.map(healthSettings));
  else for(const setting of d.settings){const node=document.querySelector(`[data-next="${setting.kind}"]`);if(node)node.textContent=setting.enabled?'Próxima execução: '+healthDate(setting.next_run_at):'Agendamento pausado.'}
  const list=$('health-runs');list.replaceChildren();
  for(const run of d.runs){const card=el('article',undefined,'health-run');const header=el('div',undefined,'flex');header.append(el('h3',healthRunName(run)),healthBadge(run.status));card.append(header,el('p',healthDate(run.started_at||run.created_at)+' · '+(run.trigger==='manual'?'Execução manual':'Agendada'),'muted'));
   if(run.configuration.bright_data)card.append(el('p','Bright Data: '+fmt(run.bright_data_requests||0)+' chamada(s) · '+fmt(run.activated||0)+' link(s) ativado(s)','muted'));const counts=run.counts||{},finished=Object.entries(counts).filter(([k])=>!['pending','running'].includes(k)).reduce((n,[,v])=>n+v,0);
   card.append(el('p',`${fmt(finished)} de ${fmt(run.total)} links concluídos`));
   const progress=el('progress');progress.max=run.total||1;progress.value=finished;progress.setAttribute('aria-label','Progresso da rodada');card.append(progress);
   const badges=el('div',undefined,'endpoint-tags');for(const [kind,count]of Object.entries(counts)){const badge=healthBadge(kind);badge.textContent=fmt(count)+' · '+(healthLabels[kind]||kind);badges.append(badge)}card.append(badges);
   const buttons=el('div',undefined,'actions');buttons.append(action('Ver resultados',()=>openHealthRun(run.id)));if(['queued','running'].includes(run.status))buttons.append(action('Cancelar rodada',async b=>{UI.mark(b,true);try{await api('/saude/rodadas/'+run.id+'/cancelar','POST',{});await loadHealth()}catch(e){alertIn('health-error',e.message)}finally{UI.mark(b,false)}}));card.append(buttons);list.append(card);
  }
  if(!d.runs.length)list.append(el('p','Nenhuma rodada executada. Use “Executar agora” ou aguarde o agendamento.','empty'));
  $('health-prev').disabled=healthOffset===0;$('health-next').disabled=healthOffset+20>=d.total;$('health-page').textContent=d.total?`${healthOffset+1}–${Math.min(healthOffset+20,d.total)} de ${fmt(d.total)} rodadas`:'Sem execuções';
  $('health-updated').textContent='Atualizado em '+new Date().toLocaleTimeString('pt-BR');alertIn('health-error','');
 }catch(e){if($('health-runs').querySelector('.skeleton-row'))$('health-runs').replaceChildren();alertIn('health-error',e.message)}finally{healthBusy=false}
}
async function openHealthRun(id,offset=0){
 healthRun=id;healthDetailOffset=offset;const serial=++healthDetailSerial;$('health-details').hidden=false;if(!$('health-results').children.length)UI.skeleton($('health-results'),3,'row');
 try{const d=await api('/saude/rodadas/'+id+'?'+new URLSearchParams({offset,category:$('health-filter').value}));if(serial!==healthDetailSerial)return;
  $('health-detail-title').textContent=healthRunName(d.run)+' · '+healthDate(d.run.created_at);$('health-detail-status').replaceChildren(healthBadge(d.run.status));
  const list=$('health-results');list.replaceChildren();
  for(const item of d.items){const card=el('article',undefined,'health-run'),snap=item.snapshot,category=item.state==='completed'?item.category:item.state;const title=el('div',undefined,'flex');title.append(el('h3',snap.source.name),healthBadge(category));card.append(title,safeLink(snap.config.url,snap.config.url));if(item.activated)card.append(el('span','Link ativado após recuperação','badge green'));
   card.append(el('p',(snap.config.kind==='feed'?'RSS / Atom':'Sitemap')+' · Modelo: '+snap.config.model+' · '+item.attempts+' tentativa(s)','muted'));if(item.result?.bright_data_enabled)card.append(el('p','Bright Data: '+fmt(item.result.bright_data_requests||0)+' chamada(s) nesta tentativa','muted'));if(item.detail)card.append(el('p',item.detail,'muted'));
   for(const attempt of item.history){const details=el('details',undefined,'evidence');details.append(el('summary','Tentativa '+attempt.attempt+' · '+(healthLabels[attempt.category]||attempt.category)+' · '+healthDate(attempt.observed_at)));if(attempt.result)drawTest(attempt.result,details);else details.append(el('p',(attempt.outcome||'sem resultado detalhado')+(attempt.status?' · HTTP '+attempt.status:''),'muted'));card.append(details)}list.append(card);
  }
  if(!d.items.length)list.append(el('p','Nenhum link neste filtro.','empty'));
  $('health-detail-prev').disabled=offset===0;$('health-detail-next').disabled=offset+25>=d.total;$('health-detail-page').textContent=d.total?`${offset+1}–${Math.min(offset+25,d.total)} de ${fmt(d.total)} links`:'Sem resultados';
 }catch(e){if(serial===healthDetailSerial){if($('health-results').querySelector('.skeleton-row'))$('health-results').replaceChildren();alertIn('health-error',e.message)}}
}
async function initHealth(){
 UI.skeleton($('health-settings'),2,'card');
 try{const d=await api('/resumo');token=d.csrf_token;await loadHealth(true)}catch(e){if($('health-settings').querySelector('.skeleton-card'))$('health-settings').replaceChildren();alertIn('health-error',e.message)}
 $('health-refresh').onclick=async()=>{await loadHealth();if(healthRun)await openHealthRun(healthRun,healthDetailOffset)};
 $('health-prev').onclick=()=>{healthOffset=Math.max(0,healthOffset-20);loadHealth()};$('health-next').onclick=()=>{healthOffset+=20;loadHealth()};
 $('health-filter').onchange=()=>{if(healthRun)openHealthRun(healthRun)};
 $('health-detail-prev').onclick=()=>openHealthRun(healthRun,Math.max(0,healthDetailOffset-25));$('health-detail-next').onclick=()=>openHealthRun(healthRun,healthDetailOffset+25);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)$('health-refresh').click()});
 setInterval(()=>{if(!document.hidden&&!document.querySelector('#health-settings :focus')){$('health-refresh').click()}},10000);
}
