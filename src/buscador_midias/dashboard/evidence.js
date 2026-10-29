import {prepareEvidenceHtml} from '/assets/evidence_prepare.js';
try{
  const response=await fetch(location.pathname+'?original=1');
  if(!response.ok)throw Error('Não foi possível abrir a evidência (HTTP '+response.status+').');
  const html=await response.text();
  document.title=new DOMParser().parseFromString(html,'text/html').title||'Evidência';
  // O aviso fica até o iframe terminar de carregar (CSS e fontes do site original), não só até o srcdoc.
  const frame=document.getElementById('evidence');
  frame.addEventListener('load',()=>{document.getElementById('loading').hidden=true},{once:true});
  frame.srcdoc=prepareEvidenceHtml(html).html;
}catch(error){
  document.getElementById('evidence').hidden=true;
  document.getElementById('loading').hidden=true;
  const message=document.getElementById('error');message.textContent=error.message;message.hidden=false;
}
