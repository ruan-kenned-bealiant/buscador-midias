import {prepareEvidenceHtml,readingEvidenceHtml} from '/assets/evidence_prepare.js';
const $=id=>document.getElementById(id),max=16*1024*1024;
let original='',request=0;
const staticHtml='<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><style>body{font:18px/1.7 system-ui;margin:40px;color:#192e29}h1{color:#176b50}</style></head><body><article><h1>Notícia de exemplo</h1><p>Texto já presente no HTML capturado por HTTP.</p></article></body></html>';
const streamed=staticHtml.replace('<article>','<!--$?--><template id="B:0"></template><p>Carregando notícia…</p><!--/$--><div hidden id="S:0"><article>').replace('</article>','</article></div><script>$RC("B:0","S:0")</script>');
function setFrame(id,html){const old=$(id),frame=old.cloneNode(false);frame.srcdoc=html;old.replaceWith(frame)}
function fail(message){$('error').textContent=message;$('error').hidden=!message}
function show(value){
 if(new Blob([value]).size>max){fail('Use um HTML de até 16 MB.');return}
 original=value;$('html').value=value;const prepared=prepareEvidenceHtml(value);
 setFrame('original',value);setFrame('prepared',$('site-layout').checked?prepared.html:readingEvidenceHtml(prepared.html));
 $('status').textContent=prepared.restored+' blocos de streaming posicionados. Nenhum script da notícia é executado.';fail('');
}
$('site-layout').onchange=()=>{document.querySelector('.compare>section:last-child h2').textContent=$('site-layout').checked?'Página capturada — layout do site':'Modo de leitura — texto extraído';show(original)};
$('compare-original').onchange=()=>document.querySelector('.compare').classList.toggle('comparing',$('compare-original').checked);
$('compare').onclick=()=>{request++;show($('html').value)};
$('static').onclick=()=>{request++;show(staticHtml)};
$('streamed').onclick=()=>{request++;show(streamed)};
$('file').onchange=async e=>{const id=++request,file=e.target.files[0];if(!file)return;if(file.size>max){fail('Use um HTML de até 16 MB.');return}try{const text=await file.text();if(id===request)show(text)}catch{fail('Não foi possível ler o arquivo.')}};
$('corpus-form').onsubmit=async e=>{
 e.preventDefault();const id=++request,match=$('hash').value.trim().match(/^(?:https?:\/\/[^/]+)?(?:\/evidencias\/corpus\/)?([a-f0-9]{64})(?:\?[^#]*)?$/);
 if(!match){fail('Informe um hash válido ou um link de evidência do corpus.');return}
 $('load-corpus').disabled=true;fail('');
 try{const r=await fetch('/evidencias/corpus/'+match[1]+'?original=1');if(!r.ok)throw Error('Evidência indisponível (HTTP '+r.status+').');if(!r.headers.get('content-type')?.startsWith('text/html'))throw Error('Selecione uma evidência HTML para esta comparação.');const text=await r.text();if(id===request)show(text)}catch(error){if(id===request)fail(error.message)}finally{$('load-corpus').disabled=false}
};
$('download').onclick=()=>{const url=URL.createObjectURL(new Blob([original],{type:'text/html;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='evidencia-original.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
show(streamed);const hash=new URLSearchParams(location.search).get('hash');if(hash){$('hash').value=hash;$('corpus-form').requestSubmit()}
