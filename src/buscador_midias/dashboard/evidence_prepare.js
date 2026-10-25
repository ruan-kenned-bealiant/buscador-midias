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
  doc.querySelectorAll('script').forEach(s=>s.remove());
  return {html:'<!doctype html>'+doc.documentElement.outerHTML,restored};
}

/** Cópia de leitura independente de CSS e imagens externos. */
export function readingEvidenceHtml(html){
  const doc=new DOMParser().parseFromString(html,'text/html');
  doc.querySelectorAll('nav,header,footer,aside,script,style,noscript,form,iframe').forEach(n=>n.remove());
  let scopes=[...doc.querySelectorAll('article')];
  if(!scopes.length)scopes=[...doc.querySelectorAll('.prose')];
  if(!scopes.length)scopes=[...doc.querySelectorAll('main')];
  if(!scopes.length)scopes=[doc.body];
  const candidates=scopes.map(scope=>[...scope.querySelectorAll('p')].filter(p=>p.textContent.trim().length>=25&&!p.closest('[hidden]')).map(p=>p.textContent.trim()));
  const paragraphs=candidates.sort((a,b)=>b.join('').length-a.join('').length)[0]||[];
  const result=document.implementation.createHTMLDocument(doc.querySelector('h1')?.textContent||doc.title||'Evidência');
  const meta=result.createElement('meta');meta.setAttribute('charset','utf-8');result.head.prepend(meta);
  const style=result.createElement('style');style.textContent='body{max-width:760px;margin:30px auto;padding:0 24px;font:18px/1.75 system-ui;color:#192e29;background:white}h1{font-size:28px;line-height:1.3}aside{font-size:13px;padding:12px;background:#edf4ee}';result.head.append(style);
  const notice=result.createElement('aside');notice.textContent='Visualização de leitura gerada do HTML capturado. O arquivo original foi preservado.';result.body.append(notice);
  const article=result.createElement('article'),heading=result.createElement('h1');heading.textContent=result.title;article.append(heading);
  for(const text of paragraphs){const p=result.createElement('p');p.textContent=text;article.append(p)}
  if(!paragraphs.length){const p=result.createElement('p');p.textContent='Não foi encontrado texto em parágrafos nesta captura. Experimente a opção Layout do site.';article.append(p)}
  result.body.append(article);return '<!doctype html>'+result.documentElement.outerHTML;
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
