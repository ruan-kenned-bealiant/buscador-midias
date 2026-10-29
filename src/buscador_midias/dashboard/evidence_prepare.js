/** Monta apenas os blocos de React já capturados. Nenhum script é executado. */
export function prepareEvidenceHtml(html){
  const doc=new DOMParser().parseFromString(html,'text/html');let restored=0;
  for(const script of doc.querySelectorAll('script:not([src])')){
    const calls=script.textContent.matchAll(/\$(RC|RR|RS)\(\s*"([BSP]:[\w.-]+)"\s*,\s*"([BSP]:[\w.-]+)"\s*[,)]/g);
    for(const call of calls){
      const [,kind,a,b]=call;
      if(kind==='RR')restoreStyles(doc,script.textContent.slice(call.index+call[0].length));
      if(kind==='RS'&&a.startsWith('S:')&&b.startsWith('P:')){
        const source=doc.getElementById(a),target=doc.getElementById(b);
        if(source&&target&&!source.contains(target)){target.replaceWith(...source.childNodes);source.remove();restored++}
      }else if((kind==='RC'||kind==='RR')&&a.startsWith('B:')&&b.startsWith('S:')){
        const target=doc.getElementById(a),source=doc.getElementById(b),opening=target?.previousSibling;
        if(!target||!source||source.contains(target)||opening?.nodeType!==8||!['$?','$~','$!'].includes(opening.textContent))continue;
        let closing=target.nextSibling,depth=0;
        while(closing){if(closing.nodeType===8){const value=closing.textContent;if(value==='/$'){if(depth===0)break;depth--}else if(['$','$?','$~','$!'].includes(value))depth++}closing=closing.nextSibling}
        if(!closing)continue;
        let node=target;
        while(node&&node!==closing){const next=node.nextSibling;node.remove();node=next}
        closing.before(...source.childNodes);source.remove();opening.textContent='$';restored++;
      }
    }
  }
  // O iframe é sandbox sem scripts, então o navegador renderizaria o <noscript>; pixels de
  // rastreio 1x1 viram um quadrado da largura da página e empurram a notícia para fora da tela.
  doc.querySelectorAll('script,noscript').forEach(s=>s.remove());
  return {html:'<!doctype html>'+doc.documentElement.outerHTML,restored};
}

/** As referências CSS de $RR são JSON; não é necessário avaliar código JS. */
function restoreStyles(doc,tail){
  const start=tail.search(/\S/);if(start<0||tail[start]!=='[')return;
  let depth=0,quoted=false,escaped=false,end=-1;
  for(let i=start;i<Math.min(tail.length,start+65536);i++){
    const c=tail[i];if(quoted){if(escaped)escaped=false;else if(c==='\\')escaped=true;else if(c==='"')quoted=false;continue}
    if(c==='"')quoted=true;else if(c==='[')depth++;else if(c===']'&&--depth===0){end=i+1;break}
  }
  if(end<0)return;
  try{
    const styles=JSON.parse(tail.slice(start,end));
    for(const entry of styles.slice(0,32)){
      if(!Array.isArray(entry)||typeof entry[0]!=='string')continue;
      const url=new URL(entry[0],doc.baseURI);if(!['http:','https:'].includes(url.protocol))continue;
      if([...doc.querySelectorAll('link[rel="stylesheet"]')].some(link=>link.href===url.href))continue;
      const link=doc.createElement('link');link.rel='stylesheet';link.href=url.href;doc.head.append(link);
    }
  }catch{/* Captura incompleta: preserva o restante da exibição. */}
}
