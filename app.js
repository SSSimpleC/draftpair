'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uuid = () => crypto.randomUUID();
const projectKey = 'qmd-margin:' + $('meta[name="review-project"]').content;
const prefKey = projectKey + ':preferences:v1';
const draftPrefix = projectKey + ':input:v1:';
function readLocal(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}
const prefs = readLocal(prefKey) || {};
const S = { ch:prefs.ch, flow:prefs.flow || 'chapter', view:prefs.view || 'auto', book:prefs.book || 'working',
  history:null, previous:false, sync:prefs.sync !== false, hl:prefs.hl !== false, side:'right',
  toc:innerWidth > 760 && prefs.toc !== false, panel:false, panelScope:'chapter', filter:null,
  order:prefs.order==='article'?'article':'recent', follow:prefs.follow===true,
  change:-1, scroll:prefs.scroll || null };
let state = {chapters:[],annotations:[],batches:[]};
let selection = null, hoverTarget = null, editor = null, draftTimer, scrollTimer, refreshBusy = false;
let editQueue = Promise.resolve(), confirmBatch = null;
let rows = [], changes = [], toastTimer;
const q = CSS.escape;
const current = () => state.chapters.find(c => c.id === S.ch);
const chapter = id => state.chapters.find(c => c.id === id);
const note = id => state.annotations.find(a => a.id === id);
const working = c => c.working || c.versions.find(v => v.id === c.round?.latest_version) || c.live;
const base = c => c.versions.find(v => v.id === c.round?.base_version) || c.live;
const byID = (c,id) => id === c.live.id ? c.live : c.versions.find(v => v.id === id);
const candidates = c => c.round ? c.versions.filter(v => v.round === c.round.id || v.id === c.round.base_version || v.id === c.round.latest_version || v.id.startsWith(c.round.id+':')).sort((a,b)=>a.number-b.number) : [];
const hasCompare = c => !!c.round && working(c).id !== base(c).id;
const isCompare = () => !!current() && S.flow === 'chapter' && S.view !== 'single' && hasCompare(current());
const vname = (c,id) => {
  if (id === c?.live.id || String(id).startsWith('live:')) return '正式正文';
  const v = c && byID(c,id);
  if (!v) return '原版本';
  return Number(v.number) === 0 ? '本轮原稿' : '第'+v.number+'稿';
};
const noLabel = c => /^[0-9]+$/.test(c.no) ? '第'+c.no+'章' : /^[A-Z]$/.test(c.no) ? '附录 '+c.no : String(c.no || '');
const cLabel = c => [noLabel(c),c.title].filter(Boolean).join(' ');
const dateLabel = d => {
  if (!d) return '';
  const date = new Date(d);
  return Number.isNaN(date.getTime()) ? String(d) : date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
};
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
function savePrefs(){
  try { localStorage.setItem(prefKey, JSON.stringify({ch:S.ch,flow:S.flow,view:S.view,book:S.book,hl:S.hl,sync:S.sync,toc:S.toc,order:S.order,follow:S.follow,scroll:S.scroll})); } catch {}
}
function toast(message, label, action){
  $('#toastTxt').textContent = message;
  const button = $('#toastAct');
  button.hidden = !label; button.textContent = label || ''; button.onclick = action || null;
  $('#toast').classList.add('on'); clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>$('#toast').classList.remove('on'),label ? 9600 : 3900);
}
function status(message, error = false){
  $('#status').textContent = message; $('#status').toggleAttribute('data-error', error);
}
function errText(error){ return String(error.message || error).slice(0,400); }
async function api(path, payload) {
  const response = await fetch('/api/'+path, payload === undefined ? {cache:'no-store'} : {
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const body = await response.json();
  if (!response.ok) {
    const reason = body.error?.message || body.message || body.error || ('请求失败 '+response.status);
    const error = new Error(typeof reason === 'string' ? reason : JSON.stringify(reason));
    error.status = response.status; throw error;
  }
  return body;
}
function take(data) {
  const next = data.state || data;
  if (!Array.isArray(next.chapters)) return false;
  state = {...next,annotations:next.annotations || [],batches:next.batches || []};
  for(const backup of backups()){
    if(!backup.saving)continue;
    const ready=payloads(backup,true).every(p=>{
      const a=note(p.id);
      return a && a.status!=='draft' && a.mode===p.mode && a.quote===p.quote && a.text===p.text && a.repl===p.repl && a.revision>p.expected_revision;
    });
    if(ready)unStash(backup);
  }
  if (!current()) S.ch = (state.chapters.find(c=>Number(c.no)===1) || state.chapters[0])?.id;
  return true;
}
async function commit(path, payload){
  const data = await api(path,payload);
  if (!take(data)) take(await api('state'));
  return data;
}
async function reload(first = false) {
  if (refreshBusy || (!first && (editor || document.querySelector('dialog[open]') || !getSelection().isCollapsed))) return;
  refreshBusy = true;
  try {
    const data = await api('state');
    const next = data.state || data;
    const changed = first || next.revision !== state.revision ||
      next.chapters.some(c=>chapter(c.id)?.live.id !== c.live.id || chapter(c.id)?.round?.latest_version !== c.round?.latest_version);
    if (changed && !editor){take(data); render(!first); if (first && S.scroll) restoreScroll(visiblePane(),S.scroll);}
    if ($('#status').hasAttribute('data-error')) status('');
  } catch(e) {
    status(errText(e),true);
    if (first) $('#stage').innerHTML = '<div class="loading">无法读取论文。 <button class="recover" id="retry">重新连接</button></div>';
    $('#retry')?.addEventListener('click',()=>reload(true),{once:true});
  } finally { refreshBusy = false; }
}
function sectionVersion(c){
  if (S.flow==='all') return S.book==='formal' ? c.live : working(c);
  return byID(c,S.history) || working(c);
}
function leftVersion(c){
  const vv = candidates(c);
  return S.previous && vv.length>2 ? vv[vv.length-2] : base(c);
}
const headingNumbers=new WeakMap();
function sectionNumbers(c,v){
  if(headingNumbers.has(v))return headingNumbers.get(v);
  const tags=new Map(),levels=[];
  if(/^[0-9]+$/.test(c.no)){
    for(const h of v.headings || []){
      if(h.level<2 || h.level>4)continue;
      const i=h.level-2;
      while(levels.length<=i)levels.push(0);
      levels[i]++;levels.length=i+1;
      tags.set(h.id,[c.no,...levels].join('.'));
    }
  }
  headingNumbers.set(v,tags);return tags;
}
function sectionHead(c,v) {
  const name = (v.id===c.live.id && !c.round) ? '实时正文' : vname(c,v.id);
  const phase = c.round && v.id!==c.live.id ? (v.number>0 ? 'revising':'annotating') : '';
  const changed = c.round && base(c).source_hash && c.live.source_hash !== base(c).source_hash;
  return '<header class="chap-head"><h1 class="chap-title">'+esc(noLabel(c) || c.title)+'</h1><div class="chap-meta">'+
    '<span class="pill '+phase+'">'+esc(name)+'</span>'+
    (v.time ? '<span>'+esc(dateLabel(v.time))+'</span>' : '')+
    (changed && v.id!==c.live.id ? '<span class="tag warn">正式正文有外部改动</span>':'')+
    '<button class="linkbtn" data-chapnote>整章意见</button></div></header>';
}
function blockHTML(b,i,flags=''){
  if (!b) return '<div class="ghost" aria-hidden="true" data-pair="'+i+'"></div>';
  return '<div class="blk '+flags+'" data-block="'+esc(b.id)+'" data-pair="'+i+'" data-kind="'+esc(b.t)+'" data-start="'+Number(b.line_start || 1)+'" data-end="'+Number(b.line_end || b.line_start || 1)+'">'+b.html+'</div>';
}
function sectionHTML(c,v,blocks){
  return '<section class="chap" data-ch="'+esc(c.id)+'" data-v="'+esc(v.id)+'">'+sectionHead(c,v)+blocks+'</section>';
}
function markedBlocks(blocks) {
  return blocks.map((b,i)=>{
    const next=blocks[i+1], empty=b.t==='heading' && (!next || next.t==='heading');
    return blockHTML(b,i)+(empty ? '<button class="empty-act" data-add="'+esc(b.id)+'">＋ 在此补写</button>':'');
  }).join('');
}
function single(c){const v=sectionVersion(c);return sectionHTML(c,v,markedBlocks(v.blocks));}

// Unchanged source blocks keep inserts from shifting every later paragraph.
function lcsPairs(a,b,equal){
  const n=a.length, m=b.length;
  if(n*m>2400000) return [];
  const dp=Array.from({length:n+1},()=>new Uint32Array(m+1));
  for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--)
    dp[i][j]=equal(a[i],b[j]) ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j],dp[i][j+1]);
  const pairs=[];
  let i=0,j=0;
  while(i<n && j<m){
    if(equal(a[i],b[j])){pairs.push([i++,j++]);}
    else if(dp[i+1][j]>=dp[i][j+1])i++;else j++;
  }
  return pairs;
}
function similarPairs(a,b){
  if(!a.length || !b.length)return [];
  if(a.length*b.length>25000)return lcsPairs(a,b,(x,y)=>x.t===y.t);
  const grams=items=>items.map(block=>{
    const str=(block.text || '').replace(/\s+/g,'');
    const set=new Set();
    for(let i=0;i<str.length;i++)set.add(str.slice(i,i+2));
    return set;
  });
  const ga=grams(a),gb=grams(b);
  const scores=a.map((left,i)=>b.map((right,j)=>{
    if(left.t!==right.t)return -1;
    const x=ga[i],y=gb[j];let common=0;
    for(const value of x)if(y.has(value))common++;
    return .2 + (x.size+y.size ? 2*common/(x.size+y.size) : 0);
  }));
  const dp=Array.from({length:a.length+1},()=>new Float64Array(b.length+1));
  for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)
    dp[i][j]=Math.max(dp[i+1][j],dp[i][j+1],scores[i][j]+dp[i+1][j+1]);
  const pairs=[];let i=0,j=0;
  while(i<a.length && j<b.length){
    if(dp[i][j]===scores[i][j]+dp[i+1][j+1])pairs.push([i++,j++]);
    else if(dp[i+1][j]>=dp[i][j+1])i++;else j++;
  }
  return pairs;
}
function paired(l,r){
  const match=(x,y)=>x.t===y.t && (x.id===y.id || (x.fingerprint && x.fingerprint===y.fingerprint));
  const anchors=lcsPairs(l,r,match);
  let l0=0,r0=0;
  const result=[];
  for(const [li,ri] of [...anchors,[l.length,r.length]]){
    const a=l.slice(l0,li), b=r.slice(r0,ri);
    let i=0,j=0;
    for(const [x,y] of [...similarPairs(a,b),[a.length,b.length]]){
      while(i<x) result.push([a[i++],null]);
      while(j<y) result.push([null,b[j++]]);
      if(x<a.length && y<b.length) result.push([a[i++],b[j++]]);
    }
    if(li<l.length) result.push([l[li],r[ri]]);
    l0=li+1;r0=ri+1;
  }
  return result;
}
function changedBlock(a,b){
  return !a || !b || (a.fingerprint && b.fingerprint ? a.fingerprint!==b.fingerprint : a.html!==b.html);
}
function wordRanges(oldText,newText){
  const a=Array.from(oldText), b=Array.from(newText);
  const offsets=chars=>{
    const result=[0];
    for(const c of chars)result.push(result[result.length-1]+c.length);
    return result;
  };
  const ao=offsets(a),bo=offsets(b);
  let p=0,e=0;
  while(p<a.length && p<b.length && a[p]===b[p])p++;
  while(e<a.length-p && e<b.length-p && a[a.length-1-e]===b[b.length-1-e])e++;
  const aa=a.slice(p,a.length-e),bb=b.slice(p,b.length-e);
  if(!aa.length && !bb.length)return [[],[]];
  const left=[],right=[];
  let x=0,y=0;
  for(const [i,j] of [...lcsPairs(aa,bb,(m,n)=>m===n),[aa.length,bb.length]]){
    if(i>x)left.push([ao[p+x],ao[p+i]]);
    if(j>y)right.push([bo[p+y],bo[p+j]]);
    x=i+1;y=j+1;
  }
  return [left,right];
}
function wrapText(block,start,end,css,id=''){
  if(!block || end<=start)return;
  const walker=document.createTreeWalker(block,NodeFilter.SHOW_TEXT);
  let offset=0, n; const slices=[];
  while((n=walker.nextNode())){
    const stop=offset+n.length;
    if(stop>start && offset<end && !n.parentElement.closest('math,svg,script,style'))
      slices.push([n,Math.max(0,start-offset),Math.min(n.length,end-offset)]);
    offset=stop;
    if(offset>=end)break;
  }
  for(const [node,lo,hi] of slices){
    let part=node;
    if(lo>0)part=node.splitText(lo);
    if(hi-lo<part.length)part.splitText(hi-lo);
    const s=document.createElement('span');s.className=css;
    if(id)s.dataset.noteId=id;
    part.before(s);s.append(part);
  }
}
function decorateChanges(){
  changes=[];
  rows.forEach(([a,b],i)=>{
    if(!changedBlock(a,b))return;
    changes.push(i);
    const l=$('.left [data-pair="'+i+'"]'),r=$('.right [data-pair="'+i+'"]');
    for(const el of [l,r])el.dataset.change=String(i);
    if(!a || !b){(a?l:r).classList.add(a?'blk-del':'blk-add');(a?r:l).dataset.kind=a?'del':'add';return;}
    l.classList.add('chg-blk');r.classList.add('chg-blk');
    const at=l.textContent,bt=r.textContent;
    if(at!==bt && ['paragraph','heading','list','blockquote','code','table'].includes(a.t)){
      const [dl,dr]=wordRanges(at,bt);
      dl.forEach(([s,e])=>wrapText(l,s,e,'diff-old'));dr.forEach(([s,e])=>wrapText(r,s,e,'diff-new'));
    } else {l.classList.add('block-old');r.classList.add('block-new');}
  });
}
function activeSections(){return $$('#stage .chap');}
function secBlock(sec,id){return id ? $('[data-block="'+q(id)+'"]',sec) : null;}
function getPos(pane){
  if(!pane)return null;
  if(pane.scrollTop<8)return {top:true,ch:$('.chap',pane)?.dataset.ch};
  const bounds=pane.getBoundingClientRect(), x=bounds.top+12;
  const blocks=$$('[data-pair]',pane);
  if(!blocks.length)return null;
  let low=0,high=blocks.length-1;
  while(low<high){const m=(low+high)>>1;if(blocks[m].getBoundingClientRect().bottom<x)low=m+1;else high=m;}
  const el=blocks[low],r=el.getBoundingClientRect();
  return {ch:el.closest('.chap').dataset.ch,id:el.dataset.block,pair:el.dataset.pair,ratio:(x-r.top)/Math.max(1,r.height)};
}
function restoreScroll(pane,pos,byPair=false){
  if(!pane || !pos)return;
  const sec=$('.chap[data-ch="'+q(pos.ch || '')+'"]',pane);
  if(!sec)return;
  const el=pos.top ? sec : (byPair ? $('[data-pair="'+q(String(pos.pair))+'"]',sec):null) || secBlock(sec,pos.id) || $('[data-pair="'+q(String(pos.pair))+'"]',sec);
  if(!el)return;
  const r=el.getBoundingClientRect();
  const delta=r.top-pane.getBoundingClientRect().top;
  pane.scrollTop=pos.top && sec===pane.querySelector('.chap') ? 0 : pane.scrollTop+delta+(pos.ratio||0)*r.height-(pos.top?0:12);
}
function visiblePane(){return $$('#stage .pane').find(p=>p.offsetWidth>0 && p.classList.contains('right')) || $$('#stage .pane').find(p=>p.offsetWidth>0);}
function adjustWidth(){
  const narrow=$('#stage').clientWidth<940;
  $('#stage').classList.toggle('narrow',narrow);$('#cbar').classList.toggle('narrow',narrow);invalidateFollow();
}
function render(keep=true){
  const c=current(); if(!c)return;
  const pos=keep ? getPos(visiblePane()) : null;
  rows=[];changes=[];S.change=-1;
  hideActions();const cmp=isCompare(),stage=$('#stage');
  stage.className='stage'+(cmp?' compare':'')+(S.hl?'':' no-hl');
  if(cmp){
    const lv=leftVersion(c),rv=working(c);
    rows=paired(lv.blocks,rv.blocks);
    stage.innerHTML=[['left',lv,0],['right',rv,1]].map(([side,v,idx])=>
      '<div class="pane editable '+side+(S.side===side?' show':'')+'" tabindex="0" aria-label="'+esc(vname(c,v.id))+'"><article class="doc">'+
      sectionHTML(c,v,rows.map((pair,i)=>blockHTML(pair[idx],i)).join(''))+'</article></div>').join('');
    decorateChanges();
  } else {
    stage.innerHTML='<div class="pane editable show" tabindex="0" aria-label="论文正文"><article class="doc">'+
     (S.flow==='all'?state.chapters.map(single).join(''):single(c))+'</article></div>';
  }
  for(const sec of activeSections()){
    const head=$('.chap-head',sec), first=head.nextElementSibling;
    const h=first?.matches('.blk') && first.querySelector(':scope > h1');
    const prefix=noLabel(chapter(sec.dataset.ch));
    if(h && prefix){h.dataset.prefix=prefix;$('.chap-title',head).replaceWith(first);}
    const c=chapter(sec.dataset.ch), numbers=sectionNumbers(c,byID(c,sec.dataset.v));
    for(const title of $$('.blk h2,.blk h3,.blk h4',sec)){
      const n=numbers.get(title.closest('.blk').dataset.block);
      if(n)title.dataset.prefix=n;
    }
  }
  followPane=null;
  wireObjects(); markAnnotations();
  for(const pane of $$('.pane',stage))pane.addEventListener('scroll',onScroll,{passive:true});
  topbar(); renderToc(); renderPanel(); adjustWidth();
  if(pos)for(const p of $$('.pane',stage))restoreScroll(p,pos);
}
function option(id,label){return '<option value="'+esc(id)+'">'+esc(label)+'</option>';}
function topbar(){
  const c=current();if(!c)return;
  $('#crumb').textContent=S.flow==='all' ? '连续全文' : cLabel(c);
  $('#crumb').title=$('#crumb').textContent;
  const cmp=isCompare(), cand=hasCompare(c);
  $$('[data-view]').forEach(b=>{b.setAttribute('aria-pressed',b.dataset.view===(cmp?'compare':'single'));b.disabled=b.dataset.view==='compare'&&(!cand||S.flow==='all');});
  $$('[data-flow]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.flow===S.flow));
  $('#cbar').classList.toggle('on',cmp);$('#rLab').textContent=vname(c,working(c).id);
  const prev=candidates(c);
  $('#leftPick').innerHTML=option('base','本轮原稿')+(prev.length>2?option('previous','上一稿 · '+vname(c,prev[prev.length-2].id)):'');
  $('#leftPick').value=S.previous && prev.length>2?'previous':'base';
  $('#compactLeft').hidden=prev.length<3;$('#compactLeft').setAttribute('aria-pressed',String(S.previous));
  for(const btn of $$('[data-side]')){btn.setAttribute('aria-pressed',S.side===btn.dataset.side);btn.textContent=btn.dataset.side==='left'?vname(c,leftVersion(c).id):vname(c,working(c).id);}
  $('#hlBtn').setAttribute('aria-pressed',S.hl);$('#syncBtn').setAttribute('aria-pressed',S.sync);
  $('#readbar').hidden=cmp || (S.flow==='chapter' && !c.versions.length);
  $('#bookLabel').hidden=S.flow!=='all';$('#versionLabel').hidden=S.flow==='all';
  $('#bookPick').value=S.book;
  $('#versionPick').innerHTML=option('','当前工作稿')+option(c.live.id,'正式正文')+[...c.versions].reverse().map(v=>
    option(v.id, (c.round && v.round===c.round.id?'':'历史 · ')+vname(c,v.id)+' '+dateLabel(v.time))).join('');
  $('#versionPick').value=S.history || '';
  const pending=allNotes().filter(submittable);
  $('#submitBtn').disabled=!pending.length;$('#submitN').textContent=String(pending.length);$('#submitN').hidden=!pending.length;
  const all=allNotes(), count=all.filter(a=>S.panelScope==='all'||a.ch===S.ch).length;
  $('#panelN').textContent=String(count);$('#panelN').hidden=!count;
  $('#panelBtn').setAttribute('aria-pressed',S.panel);$('#tocBtn').setAttribute('aria-pressed',S.toc);
  $('#app').classList.toggle('toc-off',!S.toc);$('#app').classList.toggle('panel-on',S.panel);$('#toc').inert=!S.toc;
  updateCount();
}
function renderToc(){
  $('#toc').innerHTML='<div class="toc-inner"><h4>目录</h4>'+state.chapters.map(c=>{
    const v=working(c), on=c.id===S.ch;
    const flag=c.round?(hasCompare(c)?'revising':'annotating'):'';
    const displaying=isCompare()?v:sectionVersion(c);
    const secs=on ? displaying.headings || [] : [], nums=sectionNumbers(c,displaying);
    return '<button class="toc-ch'+(on?' on':'')+'" data-ch="'+esc(c.id)+'" title="'+esc(cLabel(c))+'"><span class="dot '+flag+'"></span><span class="t">'+esc(cLabel(c))+'</span><span class="tag">'+(flag==='revising'?esc(vname(c,v.id)):flag?'批注中':'')+'</span></button>'+
      secs.filter(h=>h.level>=2 && h.level<=4).map(h=>'<button class="toc-sec" data-sec="'+esc(h.id)+'"'+(h.level>2?' style="padding-left:'+(23+11*(h.level-2))+'px"':'')+' title="'+esc(h.text)+'">'+esc([nums.get(h.id),h.text].filter(Boolean).join(' '))+'</button>').join('');
  }).join('')+'</div>';
}

const kinds={text:'选文',heading:'标题',figure:'图片',caption:'题注',formula:'公式',cell:'单元格',table:'表格',chapter:'整章'};
const statusNames={draft:'草稿',pending:'待提交',sent:'已提交',applied:'已处理',question:'待核对',archived:'已归档 · 未提交'};
function sourceAnchor(section,id,blockId){
  if(!section || !id)return null;
  const selector='[data-source-id="'+q(id)+'"]', block=secBlock(section,blockId);
  return (block && $(selector,block)) || $(selector,section);
}
function namespaceAnchors(){
  const sections=activeSections();
  for(const section of sections){
    const c=chapter(section.dataset.ch), pane=section.closest('.pane');
    const side=pane.classList.contains('left')?'left':pane.classList.contains('right')?'right':'read';
    const version=section.dataset.v===c.live.id?'live':c.versions.findIndex(v=>v.id===section.dataset.v);
    const prefix='mgt-'+side+'-c'+state.chapters.indexOf(c)+'-v'+version+'-';
    const counts=new Map();
    for(const element of $$('[id]',section)){
      const id=element.id, n=(counts.get(id)||0)+1;counts.set(id,n);
      element.dataset.sourceId=id; element.id=prefix+n+'-'+id;
    }
  }
  for(const section of sections){
    const pane=section.closest('.pane');
    for(const link of $$('a[href^="#"]',section)){
      let id=link.getAttribute('href').slice(1);try{id=decodeURIComponent(id);}catch{}
      link.dataset.sourceFragment=id;
      const ch=link.dataset.chapter || section.dataset.ch;
      const targetSection=ch===section.dataset.ch ? section : $('.chap[data-ch="'+q(ch)+'"]',pane);
      const element=sourceAnchor(targetSection,id,link.dataset.target);
      if(element)link.setAttribute('href','#'+encodeURIComponent(element.id));
    }
    for(const attr of ['aria-labelledby','aria-describedby','aria-controls','aria-owns','headers','for']){
      for(const element of $$('['+attr+']',section)){
        const adjusted=element.getAttribute(attr).split(/\s+/).map(id=>sourceAnchor(section,id)?.id || id);
        element.setAttribute(attr,adjusted.join(' '));
      }
    }
  }
}
const objectSelectors=[['h1,h2,h3,h4,h5,h6','heading'],['table','table'],['figure img,img','figure'],['figcaption,caption','caption'],['math,.math','formula'],['th,td','cell']];
function wireObjects(){
  namespaceAnchors();
  for(const sec of activeSections()){
    for(const block of $$('[data-block]',sec)){
      for(const [sel,type] of objectSelectors){
        for(const [i,e] of $$(sel,block).entries()){
          if(e.dataset.noteTarget)continue;
          e.dataset.noteTarget=type;
          e.dataset.sub=type==='cell' ? e.parentElement.rowIndex+'-'+e.cellIndex : type+'-'+i;
          if(type!=='table') e.tabIndex=0;
          if(type==='figure')e.title='图片批注 · 双击查看原图';
        }
      }
    }
  }
}
function submittable(a){return a.status==='pending' && !a.recheck && !!a.round && chapter(a.ch)?.round?.id===a.round;}
function eligibleTarget(a){ return a.status==='pending' || a.status==='draft'; }
function objectEl(sec,a){
  if(a.type==='chapter')return $('.chap-head',sec);
  const b=secBlock(sec,a.block);
  if(!b)return null;
  if(a.sub)return $('[data-sub="'+q(String(a.sub))+'"]',b);
  return b;
}
function markAnnotations(){
  for(const sec of activeSections()){
    for(const old of $$('.anno',sec).reverse())old.replaceWith(...old.childNodes);
    for(const old of $$('.has-anno',sec)){old.classList.remove('has-anno'); delete old.dataset.noteIds;delete old.dataset.count;}
    sec.normalize();
    for(const a of state.annotations){
      if(a.ch!==sec.dataset.ch || a.source_version!==sec.dataset.v || a.recheck)continue;
      if(a.anchor.type==='text'){
        for(const part of a.anchor.segs || []){
          const el=secBlock(sec,part.block);
          if(!el || el.textContent.slice(part.off,part.off+part.text.length)!==part.text)continue;
          wrapText(el,part.off,part.off+part.text.length,'anno'+(eligibleTarget(a)?'':' sent'),a.id);
        }
      } else {
        const el=objectEl(sec,a.anchor); if(!el)continue;
        const ids=[...(el.dataset.noteIds || '').split(' ').filter(Boolean),a.id];
        el.dataset.noteIds=ids.join(' ');el.dataset.count=ids.length;el.classList.add('has-anno');
      }
    }
  }
}
function hideActions(){selection=null;hoverTarget=null;$('#selBtn').classList.remove('on');$('#anchorBtn').classList.remove('on');$('#imageBtn').classList.remove('on');}
const clamp=(v,lo,hi)=>Math.max(lo,Math.min(v,hi));
function floatButton(el,rect){
  el.classList.add('on');const w=el.offsetWidth,h=el.offsetHeight;
  el.style.left=clamp(rect.right-w,6,innerWidth-w-6)+'px';
  el.style.top=clamp(rect.top-h-4,6,innerHeight-h-6)+'px';
}
function selectionChanged(){
  if(editor || document.querySelector('dialog[open]'))return;
  const sel=getSelection(), btn=$('#selBtn');
  if(sel.isCollapsed || !sel.rangeCount){btn.classList.remove('on');selection=null;return;}
  const range=sel.getRangeAt(0);
  const owner=n=>(n.nodeType===1?n:n.parentElement)?.closest('.pane');
  const pane=owner(range.startContainer);
  if(!pane || pane!==owner(range.endContainer)){btn.classList.remove('on');selection=null;return;}
  const segs=[];
  for(const b of $$('[data-block]',pane)){
    if(!range.intersectsNode(b))continue;
    const r=document.createRange();r.selectNodeContents(b);
    if(b.contains(range.startContainer))r.setStart(range.startContainer,range.startOffset);
    if(b.contains(range.endContainer))r.setEnd(range.endContainer,range.endOffset);
    const text=r.toString(); if(!text.trim())continue;
    const prefix=document.createRange();prefix.selectNodeContents(b);prefix.setEnd(r.startContainer,r.startOffset);
    const off=prefix.toString().length,sec=b.closest('.chap'),ch=sec.dataset.ch;
    segs.push({ch,source_version:sec.dataset.v,target_version:working(chapter(ch)).id,block:b.dataset.block,off,text,
      before:b.textContent.slice(Math.max(0,off-40),off),after:b.textContent.slice(off+text.length,off+text.length+40),
      line_start:Number(b.dataset.start),line_end:Number(b.dataset.end)});
  }
  if(!segs.length){selection=null;btn.classList.remove('on');return;}
  const rects=Array.from(range.getClientRects()).filter(r=>r.width && r.bottom>0 && r.top<innerHeight);
  const rect=rects.at(-1) || range.getBoundingClientRect();selection={segs,rect};
  $('#anchorBtn').classList.remove('on');$('#imageBtn').classList.remove('on');floatButton(btn,rect);
}
function newPart(ch,source,target,anchor,quote){
  return {id:uuid(),created:new Date().toISOString(),ch,source_version:source,target_version:target,anchor,quote,revision:0};
}
function selectedNote(){
  if(!selection)return;
  const {segs,rect}=selection;
  const groups=Map.groupBy ? Map.groupBy(segs,s=>s.ch) : segs.reduce((m,s)=>m.set(s.ch,[...(m.get(s.ch)||[]),s]),new Map());
  const parts=[...groups].map(([ch,ss])=>newPart(ch,ss[0].source_version,ss[0].target_version,{type:'text',segs:ss},ss.map(s=>s.text).join('\n\n')));
  begin(parts,rect); hideActions();
}
function objectNote(el,kind,forcedBlock){
  const sec=el.closest('.chap');if(!sec)return;
  const b=forcedBlock || el.closest('[data-block]');
  const ch=sec.dataset.ch;
  const sub=el.dataset.sub;
  let quote=el.textContent;
  if(kind==='chapter')quote=cLabel(chapter(ch));
  if(kind==='figure')quote=$('figcaption',b)?.textContent || el.getAttribute('alt') || '图片';
  if(kind==='formula') quote=el.getAttribute('aria-label') || quote || '公式';
  const anchor={type:kind,block:b?.dataset.block,sub,line_start:b?Number(b.dataset.start):1,line_end:b?Number(b.dataset.end):1};
  if(kind==='chapter')anchor.ch=ch;
  begin([newPart(ch,sec.dataset.v,working(chapter(ch)).id,anchor,quote.trim())],el.getBoundingClientRect());hideActions();
}
function showObject(el){
  if(!el || editor || !getSelection().isCollapsed || document.querySelector('dialog[open]'))return;
  hoverTarget=el;$('#anchorBtn').textContent='＋ '+(el.dataset.noteTarget==='heading'?'在此补充':kinds[el.dataset.noteTarget]+'批注');floatButton($('#anchorBtn'),el.getBoundingClientRect());
  const button=$('#imageBtn');button.classList.toggle('on',el.dataset.noteTarget==='figure');
  if(el.dataset.noteTarget==='figure'){
    const r=$('#anchorBtn').getBoundingClientRect();
    button.style.left=clamp(r.left-button.offsetWidth-3,5,innerWidth-button.offsetWidth-5)+'px';
    button.style.top=r.top+'px';
  }
}
let selectFrame;
document.addEventListener('selectionchange',()=>{cancelAnimationFrame(selectFrame);selectFrame=requestAnimationFrame(selectionChanged);});
for(const b of [$('#anchorBtn'),$('#selBtn'),$('#imageBtn')]) b.onpointerdown=e=>e.preventDefault();
$('#selBtn').onclick=selectedNote;
$('#anchorBtn').onclick=()=>{if(hoverTarget)objectNote(hoverTarget,hoverTarget.dataset.noteTarget);};
$('#stage').addEventListener('pointerover',e=>{
  const el=e.target.closest('[data-note-target]');
  if(e.pointerType==='mouse' && el)showObject(el);
  else if(!el){$('#anchorBtn').classList.remove('on');$('#imageBtn').classList.remove('on');}
});
$('#stage').addEventListener('focusin',e=>showObject(e.target.closest('[data-note-target]')));
function openImage(img){
  if(!img)return;
  $('#largeImage').src=img.currentSrc || img.src;$('#largeImage').alt=img.alt;$('#imageDlg').showModal();hideActions();
}
$('#imageBtn').onclick=()=>openImage(hoverTarget?.closest('img'));
$('#stage').addEventListener('dblclick',e=>openImage(e.target.closest('img')));
$('#imageClose').onclick=()=>$('#imageDlg').close();
$('#stage').addEventListener('click',e=>{
  const el=e.target.closest('.chap');if(!el || !getSelection().isCollapsed)return;
  const link=e.target.closest('a[href]');
  if(link){
    const href=link.getAttribute('href');
    if(link.dataset.chapter || href.startsWith('#')){
      e.preventDefault();
      const dest=link.dataset.chapter || el.dataset.ch;
      let sec=dest===el.dataset.ch ? el : $('.chap[data-ch="'+q(dest)+'"]',el.closest('.pane'));
      if(!sec){
        const target=chapter(dest);if(!target)return;
        const formal=el.dataset.v===chapter(el.dataset.ch).live.id;
        S.ch=target.id;S.history=formal?target.live.id:null;S.view='single';render(false);
        sec=activeSections().find(s=>s.dataset.ch===dest);
      }
      const anchor=sourceAnchor(sec,link.dataset.sourceFragment,link.dataset.target) || (sec && secBlock(sec,link.dataset.target));
      anchor?.scrollIntoView({block:'center'});
    }else if(/^https?:\/\//i.test(href)){e.preventDefault();window.open(link.href,'_blank','noopener');}
    return;
  }
  const act=e.target.closest('[data-chapnote],[data-add]');
  if(act){if(act.hasAttribute('data-chapnote'))objectNote(act,'chapter');else {const b=secBlock(el,act.dataset.add);if(b)objectNote($('[data-note-target]',b)||b,'heading',b);}return;}
  const ids=new Set();
  for(let n=e.target;n && n!==el.parentNode;n=n.parentElement){if(n.dataset.noteId)ids.add(n.dataset.noteId);for(const id of (n.dataset.noteIds || '').split(' ').filter(Boolean))ids.add(id);}
  if(ids.size){openPanel([...ids]);return;}
  const target=e.target.closest('[data-note-target]');if(target)showObject(target);
});
$('#stage').addEventListener('keydown',e=>{
  if(['Enter',' '].includes(e.key) && e.target.matches('[data-note-target]')){e.preventDefault();objectNote(e.target,e.target.dataset.noteTarget);}
});

function backups(){
  try{return Object.keys(localStorage).filter(k=>k.startsWith(draftPrefix)).map(readLocal).filter(d=>d?.parts?.length);}catch{return [];}
}
function localFor(id){return backups().find(d=>d.parts.some(p=>p.id===id || p.editing_id===id));}
function stash(d){
  if(!d.dirty)return;
  try{localStorage.setItem(draftPrefix+d.key,JSON.stringify(d));}catch{status('浏览器暂存失败，请保持页面打开',true);}
}
function unStash(d){try{localStorage.removeItem(draftPrefix+d.key);}catch{}}
function payloads(d,pending){
  return d.parts.map((p,i)=>{
    const a={...p,quote:p.quote,mode:d.mode,text:d.mode==='comment'?d.text:'',repl:d.mode==='replace'?(i===0?d.repl:''):'',
      status:pending?'pending':'draft',expected_revision:p.revision || 0};
    if(d.parts.length>1){a.group_id=d.key;a.group_index=i;a.group_count=d.parts.length;}
    if(pending && p.editing_id){
      a.id=p.editing_id;a.expected_revision=p.editing_revision;if(p.revision){a.saved_shadow_id=p.id;a.saved_shadow_revision=p.revision;}
      delete a.editing_id;delete a.editing_revision;
    }
    return a;
  });
}
function allNotes(){
  const map=new Map(state.annotations.map(a=>[a.id,a]));
  for(const d of backups()){
    for(const p of payloads(d,false)){
      if(p.editing_id)map.delete(p.editing_id);
      map.set(p.id,{...p,created:(note(p.editing_id) || note(p.id))?.created || p.created,local:true,key:d.key});
    }
  }
  for(const a of map.values())if(a.status==='draft' && a.editing_id)map.delete(a.editing_id);
  return [...map.values()];
}
function begin(parts,rect,saved){
  if(editor)fold();
  if(!saved && reselecting){
    const prior=reselecting;reselecting=null;
    if(parts.length!==1 || parts[0].ch!==prior.ch){toast('请在这条批注所属的章节重新定位');return;}
    const p=parts[0];p.editing_id=prior.id;p.editing_revision=prior.revision;p.recheck=false;
    saved={key:uuid(),parts,mode:prior.mode,text:prior.text || '',repl:prior.repl ?? '',dirty:true,seq:1};
  }
  editor=saved || {key:uuid(),parts,mode:'comment',text:'',repl:parts.map(p=>p.quote).join('\n\n'),dirty:false,seq:0};
  showEditor(rect);
}
const editorBox=$('#editor'),editorHead=$('.ed-head',editorBox);
let editorDrag=null;
function placeEditor(left,top){
  const w=editorBox.offsetWidth,h=editorBox.offsetHeight;
  editorBox.style.left=clamp(left,8,innerWidth-w-8)+'px';
  editorBox.style.top=clamp(top,8,innerHeight-h-8)+'px';
}
function endEditorDrag(){
  if(!editorDrag)return;
  const {id}=editorDrag;
  editorDrag=null;editorHead.classList.remove('dragging');
  if(editorHead.hasPointerCapture(id))editorHead.releasePointerCapture(id);
}
editorHead.addEventListener('pointerdown',e=>{
  if(!editor || editorDrag || !e.isPrimary || e.button!==0 || e.target.closest('button,.seg'))return;
  e.preventDefault();
  const rect=editorBox.getBoundingClientRect();
  editorHead.setPointerCapture(e.pointerId);
  editorDrag={id:e.pointerId,dx:e.clientX-rect.left,dy:e.clientY-rect.top};
  editorHead.classList.add('dragging');
});
editorHead.addEventListener('pointermove',e=>{
  if(editorDrag?.id===e.pointerId)placeEditor(e.clientX-editorDrag.dx,e.clientY-editorDrag.dy);
});
for(const type of ['pointerup','pointercancel','lostpointercapture']){
  editorHead.addEventListener(type,e=>{if(editorDrag?.id===e.pointerId)endEditorDrag();});
}
window.addEventListener('blur',endEditorDrag);
function showEditor(rect){
  const d=editor,c=chapter(d.parts[0].ch);
  $('#edWhere').textContent=[c?noLabel(c)||c.title:'',vname(c,d.parts[0].source_version),d.parts.length>1?'跨 '+d.parts.length+' 章':''].filter(Boolean).join(' · ');
  $('#edWhere').title=d.parts.map(p=>cLabel(chapter(p.ch))).join('、');
  $('#edQuote').textContent=d.parts.map(p=>p.quote).join('\n\n');
  $('[data-mode="replace"]').disabled=!['text','cell','caption'].includes(d.parts[0].anchor.type);
  $('#edWarn').classList.toggle('on',!!d.error || d.parts.some(p=>note(p.editing_id||p.id)?.recheck));
  $('#edWarn').textContent=d.error || '原文位置有变化，请核对引文。';
  editorBox.classList.add('on');updateMode();
  const w=editorBox.offsetWidth,h=editorBox.offsetHeight;
  const top=rect && rect.bottom+h+8<innerHeight ? rect.bottom+8 : (rect?rect.top-h-8:innerHeight-h-28);
  placeEditor(rect?rect.left:(innerWidth-w)/2,top);
  getSelection().removeAllRanges();$('#edText').focus({preventScroll:true});
}
function updateMode(){
  const d=editor,ta=$('#edText');
  for(const b of $$('[data-mode]'))b.setAttribute('aria-pressed',b.dataset.mode===d.mode);
  ta.classList.toggle('repl',d.mode==='replace');ta.value=d.mode==='replace'?d.repl:d.text;
  ta.placeholder=d.mode==='replace'?'替换后的文字（留空即删除）':'写下修改意见';
}
function draftChanged(){
  const d=editor;if(!d)return;
  d.dirty=true;d.seq++;delete d.error;stash(d);clearTimeout(draftTimer);
  status('正在暂存…');draftTimer=setTimeout(()=>enqueue(d,false),350);
}
async function sendDraft(d,pending){
  if(d.closed || !pending && (!d.dirty || d.ack===d.seq))return false;
  const seq=d.seq, list=payloads(d,pending);
  try{
    await commit(list.length===1?'annotation':'annotations',list.length===1?list[0]:{entries:list});
    const updated=list.map(a=>note(a.id));
    d.parts.forEach((p,i)=>{
      const n=updated[i];if(!n)return;
      if(!pending)p.revision=n.revision;
      p.source_version=n.source_version;p.target_version=n.target_version;p.anchor=n.anchor;
    });
    delete d.error;
    if(pending){
      d.closed=true;unStash(d);
      if(editor===d)closeBox();
      render();status('已保存');
      if(updated.some(n=>n?.recheck))toast('意见已保存，原文位置需要核对');
    } else {
      d.ack=seq;stash(d);status(d.seq===seq?'草稿已暂存':'正在暂存…');topbar();renderPanel();
    }
    return true;
  }catch(e){
    d.error=errText(e);stash(d);status('尚未同步',true);
    if(editor===d){$('#edWarn').textContent=d.error;$('#edWarn').classList.add('on');}
    toast(d.error, e.status===409?'保留为新批注':'重试',()=>{if(e.status===409)forkDraft(d);else enqueue(d,pending);});
    return false;
  }
}
function enqueue(d,pending){editQueue=editQueue.then(()=>sendDraft(d,pending)).catch(e=>{toast(errText(e));return false;});return editQueue;}
async function forkDraft(d){
  try{take(await api('state'));}catch(e){toast(errText(e));return;}
  unStash(d); d.key=uuid();d.parts=d.parts.map(p=>newPart(p.ch,p.source_version,working(chapter(p.ch)).id,p.anchor,p.quote));
  d.dirty=true;d.seq++;delete d.error;stash(d);begin(d.parts,null,d);enqueue(d,false);
}
function closeBox(){endEditorDrag();editor=null;editorBox.classList.remove('on');hideActions();getSelection().removeAllRanges();topbar();renderPanel();}
function fold(){
  const d=editor;if(!d)return;
  clearTimeout(draftTimer);
  if(d.dirty){stash(d);enqueue(d,false);}
  closeBox();
}
async function discard(){
  const d=editor;if(!d)return;
  clearTimeout(draftTimer);
  if(d.dirty && !window.confirm('放弃这次输入？'))return;
  d.closed=true;await editQueue;
  try{
    for(const p of d.parts){const a=note(p.id);if(a?.status==='draft')await commit('annotation/delete',{id:a.id,expected_revision:a.revision});}
    unStash(d);closeBox();render();status('');
  }catch(e){d.closed=false;stash(d);toast(errText(e));}
}
async function saveEditor(){
  const d=editor;if(!d || d.saving)return;
  const original=d.parts.map(p=>p.quote).join('\n\n');
  if(d.mode==='comment' && !d.text.trim() || d.mode==='replace' && d.repl===original){
    toast(d.mode==='replace'?'替换文字与原文相同':'请先填写意见');$('#edText').focus();return;
  }
  clearTimeout(draftTimer);d.dirty=true;d.saving=true;stash(d);
  $('#edSave').disabled=true;$('#edText').readOnly=true;
  try{await enqueue(d,true);}finally{d.saving=false;$('#edSave').disabled=false;$('#edText').readOnly=false;if(!d.closed)stash(d);}
}
for(const b of $$('[data-mode]'))b.onclick=()=>{if(!editor || editor.mode===b.dataset.mode)return;editor.mode=b.dataset.mode;updateMode();draftChanged();$('#edText').focus();};
$('#edText').oninput=e=>{if(editor){editor[editor.mode==='replace'?'repl':'text']=e.target.value;draftChanged();}};
const editorText=$('#edText');
let composingNote=false,compositionEndTimer=0;
function clearCompositionEnd(){
  clearTimeout(compositionEndTimer);compositionEndTimer=0;
}
editorText.addEventListener('compositionstart',()=>{clearCompositionEnd();composingNote=true;});
editorText.addEventListener('compositionend',()=>{
  // WebKit can finish composition before delivering the Enter that confirmed the candidate.
  composingNote=false;
  compositionEndTimer=setTimeout(clearCompositionEnd,0);
});
editorText.addEventListener('keyup',clearCompositionEnd);
editorText.onkeydown=e=>{
  if(composingNote || e.isComposing || e.keyCode===229)return;
  if(e.key==='Enter' && !e.shiftKey && !e.altKey){
    e.preventDefault();
    if(compositionEndTimer){clearCompositionEnd();return;}
    if(!e.repeat)saveEditor();
  }else if(e.key==='Escape'){
    e.preventDefault();fold();
  }
};
$('#edFold').onclick=fold;$('#edCancel').onclick=discard;$('#edSave').onclick=saveEditor;
document.addEventListener('pointerdown',e=>{if(editor && !editor.saving && !e.target.closest('#editor,#toast,dialog'))fold();});

let reselecting=null;
const editableRow = a => a.status==='draft' || (a.status==='pending' && chapter(a.ch)?.round?.id===a.round);
const creationTime=a=>(a.editing_id && note(a.editing_id)?.created) || a.created || a.at;
function blockPositions(ch,version){
  // Across revisions, place deleted paragraphs immediately before the next working paragraph.
  const latest=working(ch);
  const pairs=version.id===latest.id || version.source_hash && version.source_hash===latest.source_hash
    ? version.blocks.map(b=>[b,b]) : paired(version.blocks,latest.blocks);
  const positions=new Map();let next=0;
  for(const [left,right] of pairs){
    if(left)positions.set(left.id,{block:left,index:next,deleted:right?0:-1});
    if(right)next++;
  }
  return positions;
}
function objectOffset(block,anchor,templates){
  if(!block || !anchor.sub)return -1;
  let root=templates.get(block);
  if(!root){
    const template=document.createElement('template');template.innerHTML=block.html;root=template.content;
    templates.set(block,root);
  }
  let target;
  if(anchor.type==='cell'){
    const [row,col]=anchor.sub.split('-').map(Number);
    target=$('table',root)?.rows[row]?.cells[col];
  }else{
    const selector=objectSelectors.find(([,type])=>type===anchor.type)?.[0];
    if(selector)target=$$(selector,root)[Number(anchor.sub.split('-').at(-1))];
  }
  if(!target)return -1;
  const range=document.createRange();range.selectNodeContents(root);range.setEndBefore(target);
  return range.toString().length;
}
function sortNotes(notes){
  const times=new Map(notes.map(a=>[a,Date.parse(creationTime(a)) || (a.local?Number.MAX_SAFE_INTEGER:0)]));
  const recent=(a,b)=>times.get(b)-times.get(a) || a.id.localeCompare(b.id);
  if(S.order==='recent')return notes.sort(recent);
  const positions=new Map(),templates=new Map(),keys=new Map();
  for(const a of notes){
    const c=chapter(a.ch),v=c && byID(c,a.source_version);
    if(v && !positions.has(v))positions.set(v,blockPositions(c,v));
    const anchor=a.anchor,part=anchor.segs?.[0] || anchor;
    const loc=positions.get(v)?.get(part.block);
    const line=part.line_start ?? a.line_start ?? Number.MAX_SAFE_INTEGER;
    let index=loc?.index;
    if(index==null && c){
      const next=working(c).blocks.findIndex(b=>b.line_end>=line);
      index=next<0?working(c).blocks.length:next;
    }
    keys.set(a,[state.chapters.indexOf(c),anchor.type==='chapter'?-1:index,loc?.deleted || 0,
      loc?.deleted?line:0,part.off ?? objectOffset(loc?.block,anchor,templates),anchor.type==='text'?1:0]);
  }
  return notes.sort((a,b)=>{
    const left=keys.get(a),right=keys.get(b);
    for(let i=0;i<left.length;i++)if(left[i]!==right[i])return left[i]-right[i];
    return recent(a,b);
  });
}

const following=()=>S.panel && S.order==='article' && S.follow && !S.filter;
let followGroups=[],followCards=[],followPane=null,followFrame=0,rail=null,followDirty=true;
const followResize=new ResizeObserver(()=>invalidateFollow());
function quoteRange(block,part){
  const end=part.off+part.text.length;
  if(!part.text || block.textContent.slice(part.off,end)!==part.text)return null;
  const walker=document.createTreeWalker(block,NodeFilter.SHOW_TEXT),range=document.createRange();
  let offset=0,started=false,node;
  while((node=walker.nextNode())){
    const next=offset+node.length;
    if(!started && part.off<next){range.setStart(node,part.off-offset);started=true;}
    if(started && end<=next){range.setEnd(node,end-offset);return range;}
    offset=next;
  }
  return null;
}
function collectFollowTargets(notes){
  const groups=[];
  for(const section of activeSections()){
    const c=chapter(section.dataset.ch),shown=byID(c,section.dataset.v),blocks=new Map();
    for(const a of notes){
      if(a.ch!==c.id || a.recheck)continue;
      const source=byID(c,a.source_version),sameVersion=a.source_version===shown.id;
      if(!sameVersion && (!source?.source_hash || source.source_hash!==shown.source_hash))continue;
      for(const part of a.anchor.segs || [a.anchor]){
        const block=a.anchor.type==='chapter'?$('.chap-head',section):secBlock(section,part.block);
        if(!block)continue;
        if(!sameVersion && ['figure','table','formula','cell','caption'].includes(a.anchor.type)){
          const original=source.blocks.find(b=>b.id===part.block),displayed=shown.blocks.find(b=>b.id===part.block);
          if(original?.fingerprint!==displayed?.fingerprint)continue;
        }
        const target=a.anchor.type==='text'?quoteRange(block,part):objectEl(section,a.anchor);
        if(!target)continue;
        if(!blocks.has(block))blocks.set(block,[]);
        blocks.get(block).push({id:a.id,target});
      }
    }
    if(blocks.size)groups.push({pane:section.closest('.pane'),blocks});
  }
  return groups;
}
const intersects=(a,b)=>a.bottom>b.top && a.top<b.bottom && a.right>b.left && a.left<b.right;
// Content coordinates stay fixed while either native scrollport moves.
function followExtents(pane){
  const extents=new Map(),origin=pane.getBoundingClientRect().top-pane.scrollTop;
  const pairs=new Map($$('[data-pair]',pane).map(el=>[el.dataset.pair,el]));
  for(const group of followGroups){
    const source=group.pane;
    if(!source.clientWidth || (source!==pane && !S.sync))continue;
    for(const [block,targets] of group.blocks){
      const rect=block.getBoundingClientRect();
      let top=rect.top-origin,scale=1;
      if(source!==pane){
        const other=block.matches('.chap-head')?$('.chap-head',pane):pairs.get(block.dataset.pair);
        if(!other)continue;
        const mapped=other.getBoundingClientRect();
        top=mapped.top-origin;scale=mapped.height/Math.max(1,rect.height);
      }
      for(const {id,target} of targets){
        const lines=Array.from(target.getClientRects()).filter(r=>r.width && r.height && r.right>rect.left && r.left<rect.right);
        if(!lines.length)continue;
        const first=Math.min(...lines.map(r=>r.top)),last=Math.max(...lines.map(r=>r.bottom));
        if(!extents.has(id))extents.set(id,[]);
        extents.get(id).push([top+(first-rect.top)*scale,top+(last-rect.top)*scale]);
      }
    }
  }
  return extents;
}
function buildFollowRail(pane){
  const body=$('#pBody'),track=$('.follow-track',body),height=body.clientHeight;
  const scale=height/Math.max(1,pane.clientHeight),extents=followExtents(pane),items=[];
  for(const card of followCards){
    const spans=extents.get(card.dataset.id);
    card.hidden=!spans;
    if(!spans)card.classList.remove('at-source');
    if(spans)items.push({card,spans,start:Math.min(...spans.map(s=>s[0])),end:Math.max(...spans.map(s=>s[1]))});
  }
  items.sort((a,b)=>a.start-b.start);
  for(const item of items)item.height=item.card.getBoundingClientRect().height;
  // Reserve extra scroll distance for overlapping cards; isolated notes travel with their line.
  const groups=[],gap=8;
  for(const item of items){
    const y=item.start*scale;
    let group=groups.at(-1);
    if(!group || y>=group.start+group.height+gap || y-group.start>height*.55){
      group={start:y,height:0,items:[]};groups.push(group);
    }
    item.offset=Math.max(y-group.start,group.height);
    group.height=item.offset+item.height+gap;group.items.push(item);
  }
  const knots=[{x:0,y:0}];let extra=0;
  for(let i=0;i<groups.length;i++){
    const group=groups[i],x=group.start,y=x+extra;
    for(const item of group.items){
      item.top=y+item.offset+6;item.card.style.top=item.top+'px';
      item.card.style.transform='';
    }
    const run=Math.min(height*.85,(groups[i+1]?.start ?? Infinity)-x);
    const overflow=Math.max(0,group.height-run);
    if(overflow>0){
      knots.push({x,y},{x:x+run,y:y+run+overflow});extra+=overflow;
    }
  }
  const naturalHeight=pane.scrollHeight*scale,last=items.at(-1);
  // Leave enough room below the last card for its quote to reach the reading line.
  track.style.height=(last?Math.max(naturalHeight+extra+12,last.top+Math.max(height,last.height+12)):0)+'px';
  $('#pEmpty').hidden=!!items.length;
  const origin=pane.getBoundingClientRect().top-pane.scrollTop;
  const sections=S.flow==='all' && S.panelScope==='chapter'?$$('.chap',pane).map(el=>{
    const r=el.getBoundingClientRect();return {start:r.top-origin,end:r.bottom-origin};
  }):[];
  return {pane,scale,knots,items,sections,goal:null,manualOffset:0,manualAt:0,written:body.scrollTop,lastFrame:0,docY:pane.scrollTop};
}
function railPosition(view,documentY){
  const x=Math.max(0,documentY)*view.scale,points=view.knots;
  let low=0,high=points.length;
  while(low+1<high){const mid=(low+high)>>1;if(points[mid].x<=x)low=mid;else high=mid;}
  const a=points[low],b=points[low+1];
  return b?a.y+(x-a.x)*(b.y-a.y)/(b.x-a.x):a.y+x-a.x;
}
function readingRailPosition(view){
  return railPosition(view,view.pane.scrollTop+view.pane.clientHeight/3);
}
function updateFollowCount(){
  if(!rail || !following())return;
  const body=$('#pBody'),top=body.scrollTop,bottom=top+body.clientHeight;
  const docTop=rail.pane.scrollTop,docBottom=docTop+rail.pane.clientHeight;
  let total=0,first=0,last=0;
  for(const [i,item] of rail.items.entries()){
    const visible=item.spans.some(([start,end])=>end>=docTop && start<docBottom);
    item.card.classList.toggle('at-source',visible);
    // A long selection can stay in view until the next card needs the space.
    let y=item.top;
    if(item.end-item.start>rail.pane.clientHeight){
      const limit=Math.min(railPosition(rail,item.end),rail.items[i+1]?.top ?? Infinity)-item.height-8;
      y+=clamp(top+6-y,0,Math.max(0,limit-y));
      item.card.style.transform='translateY('+(y-item.top)+'px)';
    }
    if(visible){
      total++;
      if(y+item.height>top+2 && y<bottom-2){if(!first)first=total;last=total;}
    }
  }
  let label=total?'本屏批注　'+total+' 条':'本屏暂无批注';
  if(first && last-first+1<total)label='本屏批注　'+(first===last?first:first+'–'+last)+' / '+total;
  if($('#pRange').textContent!==label)$('#pRange').textContent=label;
}
function updateFollow(now){
  followFrame=0;
  if(!following())return;
  const pane=followPane?.isConnected && followPane.clientWidth?followPane:visiblePane();
  if(!pane)return;
  if(followDirty || !rail || rail.pane!==pane){
    const pin=rail?.pane===pane && rail.manualOffset?currentPanelPosition():null;
    rail=buildFollowRail(pane);followDirty=false;
    const item=pin && rail.items.find(item=>item.card.dataset.id===pin.id);
    if(item){rail.manualAt=pane.scrollTop;rail.manualOffset=item.top-pin.offset-readingRailPosition(rail);}
  }
  const body=$('#pBody'),docY=pane.scrollTop,base=readingRailPosition(rail);
  // Let a reader finish a dense cluster; blend out their manual offset with document travel.
  const hold=Math.max(0,1-Math.abs(docY-rail.manualAt)/Math.max(1,pane.clientHeight*.6));
  if(!hold)rail.manualOffset=0;
  const goal=clamp(base+rail.manualOffset*hold,0,body.scrollHeight-body.clientHeight);
  const delta=goal-body.scrollTop,frameTime=now-rail.lastFrame;
  const elapsed=frameTime>50?16:clamp(frameTime,4,50);
  const step=Math.abs(docY-rail.docY);
  // Small gestures track 1:1. Wheel steps settle briefly; navigation jumps go straight there.
  const snap=rail.goal===null || reducedMotion() || (step>0 && step<=32) || Math.abs(delta)<=8 || step>pane.clientHeight*.9;
  const position=snap?goal:body.scrollTop+delta*(1-Math.exp(-elapsed/24));
  body.scrollTop=position;rail.written=body.scrollTop;rail.goal=goal;rail.docY=docY;rail.lastFrame=now;
  updateFollowCount();
  if(Math.abs(goal-rail.written)>1)scheduleFollow();
}
function scheduleFollow(){
  if(following() && !followFrame)followFrame=requestAnimationFrame(updateFollow);
}
function invalidateFollow(){followDirty=true;scheduleFollow();}
$('#pBody').addEventListener('scroll',()=>{
  if(!rail || !following())return;
  const body=$('#pBody');
  if(Math.abs(body.scrollTop-rail.written)>1){
    const docY=rail.pane.scrollTop,base=readingRailPosition(rail);
    if(rail.sections.length){
      const chapters=rail.sections.filter(s=>s.end>docY && s.start<docY+rail.pane.clientHeight);
      if(chapters.length)body.scrollTop=clamp(body.scrollTop,Math.min(base,railPosition(rail,chapters[0].start)),
        Math.max(base,railPosition(rail,chapters.at(-1).end)-body.clientHeight));
    }
    rail.manualAt=docY;rail.manualOffset=body.scrollTop-base;rail.written=body.scrollTop;
    rail.goal=body.scrollTop;
  }
  updateFollowCount();
},{passive:true});
function setFollowPane(e){
  const pane=e.target.closest('.pane');
  if(pane && pane!==followPane){followPane=pane;scheduleFollow();}
}
$('#stage').addEventListener('pointerdown',setFollowPane);
$('#stage').addEventListener('focusin',setFollowPane);
$('#stage').addEventListener('load',invalidateFollow,true);
$('#stage').addEventListener('scroll',e=>{if(!e.target.matches('.pane'))invalidateFollow();},true);
document.fonts.ready.then(invalidateFollow);
function openPanel(ids=null){
  S.panel=true;S.filter=ids;S.panelScope=ids?'all':S.panelScope;
  if(innerWidth<=1150 && S.toc)S.toc=false;
  topbar();renderPanel();
}
function currentPanelPosition(){
  const body=$('#pBody'),bounds=body.getBoundingClientRect();
  const item=$$('.item:not([hidden])',body).find(el=>intersects(el.getBoundingClientRect(),bounds));
  return item && {id:item.dataset.id,offset:item.getBoundingClientRect().top-bounds.top};
}
function renderPanel(pin=null){
  const panel=$('#panel');panel.hidden=!S.panel;panel.classList.toggle('on',S.panel);
  followResize.disconnect();rail=null;followGroups=[];followCards=[];
  if(!S.panel)return;
  for(const b of $$('[data-order]'))b.setAttribute('aria-pressed',b.dataset.order===S.order);
  $('#followText').checked=S.follow;
  $('#followOption').hidden=S.order!=='article' || !!S.filter;
  panel.classList.toggle('following',following());
  for(const b of $$('[data-pf]'))b.setAttribute('aria-pressed',b.dataset.pf===S.panelScope);
  $('#pFilter').classList.toggle('on',!!S.filter);$('#pFilterTxt').textContent=S.filter?'所选位置 · '+S.filter.length+' 条':'';
  const continuous=following() && S.flow==='all';
  const notes=allNotes().filter(a=>(continuous || S.panelScope==='all' || a.ch===S.ch) && (!S.filter || S.filter.includes(a.id) || S.filter.includes(a.editing_id)));
  let html='';
  const ordered=sortNotes(notes);
  for(const a of ordered){
    const c=chapter(a.ch);if(!c)continue;
    const st=a.status==='pending' && c.round?.id!==a.round?'archived':a.status;
    const created=creationTime(a);
    const statusLabel=st==='applied' && a.applied_version?'已落实到'+vname(c,a.applied_version):statusNames[st] || st;
    const meta=[noLabel(c)||c.title,vname(c,a.source_version),kinds[a.anchor.type] || '选文'].map(esc).join(' · ');
    const action='<button data-na="jump">定位</button>'+
      (editableRow(a)?'<button data-na="edit">'+(st==='draft'?'继续输入':'修改')+'</button><button data-na="remove" class="danger">'+(st==='draft'?'放弃草稿':'撤回')+'</button>':'')+
      (a.recheck && st==='pending'?'<button data-na="reselect">重新定位</button>':'')+
      (st==='question'?'<button data-na="reply">补充意见</button>':'')+
      (st==='applied' && a.applied_version?'<button data-na="applied">查看落实稿</button>':'');
    html+='<article class="item" data-id="'+esc(a.id)+'"><div class="item-meta">'+meta+
      (a.group_id?'<span class="tag">跨章 '+(Number(a.group_index)+1)+'/'+a.group_count+'</span>':'')+
      (a.batch?'<span class="tag">批次 '+esc(a.batch)+'</span>':'')+
      (a.recheck?'<span class="tag warn">需核对</span>':'')+
      (created?'<time style="margin-left:auto" title="批注创建时间">'+esc(dateLabel(created))+'</time>':'')+
      '<span class="tag">'+esc(statusLabel)+'</span></div><blockquote>'+esc(a.quote)+'</blockquote><div class="txt">'+
      (a.mode==='replace'?'<span class="tag repl">逐字替换</span> <span class="rp">'+esc(a.repl || '删除选文')+'</span>':esc(a.text))+
      (a.question?'<div class="ed-quote">'+esc(a.question)+'</div>':'')+'</div><div class="item-act">'+action+'</div></article>';
  }
  const body=$('#pBody');
  body.innerHTML=(following()?'<div class="follow-track">'+html+'</div>':html)+'<div class="p-empty" id="pEmpty" hidden></div>';
  $('#pEmpty').textContent=following()?'当前稿暂无可定位的批注':'暂无批注';
  $('#pRange').hidden=!following();
  if(following()){
    followCards=$$('.item',body);
    followGroups=collectFollowTargets(ordered);
    followResize.observe(body);
    for(const doc of $$('#stage .doc'))followResize.observe(doc);
    scheduleFollow();
  }else{
    $('#pEmpty').hidden=!!html;
  }
  if(pin){
    const item=$('.item[data-id="'+q(pin.id)+'"]:not([hidden])',body);
    if(item)body.scrollTop+=item.getBoundingClientRect().top-body.getBoundingClientRect().top-pin.offset;
  }
}
function jump(a){
  const c=chapter(a.ch);if(!c)return;
  S.ch=c.id;
  let target=activeSections().find(s=>s.dataset.ch===c.id && s.dataset.v===a.source_version && s.offsetWidth);
  if(!target){
    S.flow='chapter';S.view='single';S.history=byID(c,a.source_version)?.id || (a.source_version?.startsWith('live:')?c.live.id:null);
    if(!S.history && !a.source_version?.startsWith('live:')){toast('当前记录缺少所引版本');return;}
    render(false);target=activeSections()[0];
  }
  if(!target)return;
  let ref=$('[data-note-id="'+q(a.id)+'"]',target) || objectEl(target,a.anchor);
  if(!ref && a.anchor.segs?.length)ref=secBlock(target,a.anchor.segs[0].block);
  if(!ref){toast('原位置有变化，请按引文重新定位');return;}
  ref.scrollIntoView({block:'center',behavior:'instant'});ref.classList.remove('flash');void ref.offsetHeight;ref.classList.add('flash');
  $$('.anno.focus').forEach(s=>s.classList.remove('focus'));$$('[data-note-id="'+q(a.id)+'"]',target).forEach(s=>s.classList.add('focus'));
  return ref;
}
function jumpApplied(a){
  const c=chapter(a.ch),v=byID(c,a.applied_version),source=byID(c,a.source_version);
  if(!v || !source){toast('缺少所引版本');return;}
  const sourceId=a.anchor.block || a.anchor.segs?.[0]?.block;
  const pairs=paired(source.blocks,v.blocks);
  const index=pairs.findIndex(([old])=>old?.id===sourceId);
  const corresponding=index<0 ? null : pairs[index][1] || pairs.slice(index+1).find(pair=>pair[1])?.[1] || pairs.slice(0,index).reverse().find(pair=>pair[1])?.[1];
  S.ch=c.id;S.flow='chapter';S.history=v.id;S.view='single';render(false);
  const el=corresponding ? secBlock(activeSections()[0],corresponding.id) : $('.chap-head');
  el?.scrollIntoView({block:'center'});el?.classList.add('flash');
}
function resumeNote(a){
  const stashData=localFor(a.id);
  const rect=jump(a)?.getBoundingClientRect() || null;
  if(stashData){
    delete stashData.saving;delete stashData.closed;
    const expected=payloads(stashData,false);
    let allEqual=true;
    stashData.parts.forEach((p,i)=>{
      const n=note(p.id), e=expected[i];
      const same=n?.status==='draft' && n.source_version===e.source_version && n.mode===e.mode && n.text===e.text && n.repl===e.repl && n.quote===e.quote;
      if(same)p.revision=n.revision;
      else allEqual=false;
    });
    if(allEqual)stashData.ack=stashData.seq;
    stash(stashData);begin(stashData.parts,rect,stashData);return;
  }
  let linked=a.group_id ? state.annotations.filter(n=>n.group_id===a.group_id && n.status===a.status).sort((x,y)=>x.group_index-y.group_index):[a];
  if(linked.length!==Number(a.group_count || 1))linked=[a];
  const parts=linked.map(p=>p.status==='draft'?{...p}:{...newPart(p.ch,p.source_version,p.target_version,p.anchor,p.quote),editing_id:p.id,editing_revision:p.revision,recheck:p.recheck});
  const d={key:a.group_id || uuid(),parts,mode:a.mode,text:linked[0].text || '',repl:linked[0].repl ?? a.quote,dirty:a.status==='draft',seq:0,ack:0};
  begin(parts,rect,d);
}
async function removeNote(a){
  if(!confirm(a.status==='draft'?'放弃这条草稿？':'撤回这条待提交批注？'))return;
  clearTimeout(draftTimer);await editQueue;
  try{
    const d=localFor(a.id);
    const list=d ? d.parts : [a];
    for(const p of list){const existing=note(p.id);if(existing && eligibleTarget(existing))await commit('annotation/delete',{id:p.id,expected_revision:existing.revision});}
    if(d)unStash(d);
    S.filter=null;render();toast('已撤回');
  }catch(e){toast(errText(e));}
}
$('#pBody').onclick=e=>{
  const row=e.target.closest('.item');if(!row)return;
  const a=allNotes().find(n=>n.id===row.dataset.id);if(!a)return;
  const act=e.target.closest('[data-na]')?.dataset.na || 'jump';
  if(act==='jump')jump(a);
  if(act==='applied')jumpApplied(a);
  if(act==='edit')resumeNote(a);
  if(act==='remove')removeNote(a);
  if(act==='reselect'){
    reselecting=a;S.ch=a.ch;S.flow='chapter';S.history=null;S.view='single';S.panel=false;render(false);
    toast('重新选中要修改的文字，或点选图表。','取消定位',()=>{reselecting=null;$('#toast').classList.remove('on');});
  }
  if(act==='reply'){
    const c=chapter(a.ch);const p=newPart(a.ch,a.source_version,working(c).id,a.anchor,a.quote);
    p.context='回复批注 '+a.id;
    begin([p],jump(a)?.getBoundingClientRect());
  }
};
for(const b of $$('[data-order]'))b.onclick=()=>{
  if(S.order===b.dataset.order)return;
  S.order=b.dataset.order;$('#pBody').scrollTop=0;renderPanel();savePrefs();
};
$('#followText').onchange=e=>{
  const pin=S.follow?currentPanelPosition():null;
  S.follow=e.target.checked;$('#pBody').scrollTop=0;renderPanel(pin);savePrefs();
};
for(const b of $$('[data-pf]'))b.onclick=()=>{S.panelScope=b.dataset.pf;S.filter=null;renderPanel();topbar();};
$('#pFilterClear').onclick=()=>{S.filter=null;renderPanel();};
$('#panelClose').onclick=()=>{S.panel=false;S.filter=null;topbar();renderPanel();};
$('#panelBtn').onclick=()=>S.panel?$('#panelClose').click():openPanel();
$('#submitBtn').onclick=async()=>{
  await editQueue;
  const shadows=new Set(allNotes().filter(a=>a.status==='draft' && a.editing_id).map(a=>a.editing_id));
  const list=state.annotations.filter(a=>submittable(a) && !shadows.has(a.id) && !localFor(a.id));
  if(!list.length){toast('尚无可提交的批注');return;}
  confirmBatch={key:uuid(),entries:list.map(n=>({id:n.id,revision:n.revision}))};
  $('#dlgTitle').textContent='提交 '+list.length+' 条批注';
  $('#dlgSub').textContent=[...new Set(list.map(a=>cLabel(chapter(a.ch))))].join('、');
  $('#dlgBody').innerHTML=list.map((a,i)=>'<div class="dlg-row"><span class="no">'+(i+1)+'</span><div><div class="q">'+esc(a.quote)+'</div><div>'+esc(a.mode==='replace'?(a.repl?'替换为「'+a.repl+'」':'删除选文'):a.text)+'</div></div></div>').join('');
  $('#submitDlg').showModal();
};
$('#dlgCancel').onclick=()=>$('#submitDlg').close();
$('#dlgOk').onclick=async()=>{
  if(!confirmBatch || $('#dlgOk').disabled)return;
  $('#dlgOk').disabled=true;
  try{
    const result=await commit('submit',confirmBatch);
    const latest=result.batch || state.batches.at(-1);
    $('#submitDlg').close();confirmBatch=null;render();status('已提交');
    toast('批次 '+(latest?.number || latest?.id || '')+' 已提交','复制给 Codex',async()=>{
      try{await navigator.clipboard.writeText('请处理论文审阅工具中的最新批注。');toast('已复制');}catch{toast('请在论文聊天中说「处理最新批注」');}
    });
  }catch(e){toast(errText(e));if(e.status===409){$('#submitDlg').close();confirmBatch=null;await reload();}}
  finally{$('#dlgOk').disabled=false;}
};
$('#submitDlg').onclose=()=>{confirmBatch=null;};
function updateCount(){
  $('#chgCount').textContent=changes.length ? (S.change<0?changes.length+' 处改动':(S.change+1)+' / '+changes.length) : '无改动';
  $('#chgPrev').disabled=$('#chgNext').disabled=!changes.length;
}
let syncOwner=null, releaseFrame;
function onScroll(e){
  const pane=e.currentTarget;
  $('#selBtn').classList.remove('on');$('#anchorBtn').classList.remove('on');$('#imageBtn').classList.remove('on');
  if(syncOwner && syncOwner!==pane)return;
  followPane=pane;scheduleFollow();
  if(isCompare() && S.sync){
    syncOwner=pane;
    const other=$$('.pane').find(p=>p!==pane && p.offsetWidth);
    if(other)restoreScroll(other,getPos(pane),true);
    cancelAnimationFrame(releaseFrame);releaseFrame=requestAnimationFrame(()=>{releaseFrame=requestAnimationFrame(()=>{syncOwner=null;});});
  }
  clearTimeout(scrollTimer);
  scrollTimer=setTimeout(()=>{
    S.scroll=getPos(pane);
    if(S.flow==='all' && S.scroll?.ch && S.scroll.ch!==S.ch){S.ch=S.scroll.ch;renderToc();topbar();if(!following())renderPanel();}
    const sec=$('.chap[data-ch="'+q(S.ch)+'"]',pane);
    if(sec){
      const top=pane.getBoundingClientRect().top+45;
      const heads=$$('.blk[data-kind="heading"]',sec);
      const active=heads.filter(h=>h.getBoundingClientRect().top<=top).at(-1);
      for(const button of $$('#toc [data-sec]')){
        if(button.dataset.sec===active?.dataset.block)button.setAttribute('aria-current','location');else button.removeAttribute('aria-current');
      }
    }
    savePrefs();
  },160);
}
function gotoChange(step){
  if(!changes.length)return;
  S.change=S.change<0 ? (step>0?0:changes.length-1) : (S.change+step+changes.length)%changes.length;
  for(const old of $$('.cur-chg'))old.classList.remove('cur-chg');
  for(const el of $$('[data-change="'+changes[S.change]+'"]')){
    el.classList.add('cur-chg');if(el.offsetWidth)el.scrollIntoView({block:'center',behavior:'instant'});
  }
  updateCount();
}
$('#chgPrev').onclick=()=>gotoChange(-1);$('#chgNext').onclick=()=>gotoChange(1);
$('#hlBtn').onclick=()=>{S.hl=!S.hl;$('#stage').classList.toggle('no-hl',!S.hl);topbar();savePrefs();};
$('#syncBtn').onclick=()=>{S.sync=!S.sync;topbar();invalidateFollow();savePrefs();};
for(const b of $$('[data-side]'))b.onclick=()=>{
  const p=getPos(visiblePane());S.side=b.dataset.side;
  for(const pane of $$('.pane'))pane.classList.toggle('show',pane.classList.contains(S.side));
  restoreScroll(visiblePane(),p,true);topbar();scheduleFollow();
};
$('#compactLeft').onclick=()=>{S.previous=!S.previous;render();};
$('#leftPick').onchange=e=>{S.previous=e.target.value==='previous';render();};
$('#bookPick').onchange=e=>{S.book=e.target.value;render();savePrefs();};
$('#versionPick').onchange=e=>{S.history=e.target.value || null;render();};
$('#tocBtn').onclick=()=>{S.toc=!S.toc;if(S.toc && innerWidth<1150)S.panel=false;topbar();renderPanel();savePrefs();};
for(const b of $$('[data-view]'))b.onclick=()=>{S.view=b.dataset.view;S.history=null;render();savePrefs();};
for(const b of $$('[data-flow]'))b.onclick=()=>{
  const pos=getPos(visiblePane());if(pos?.ch)S.ch=pos.ch;
  S.flow=b.dataset.flow;S.history=null;render(false);
  for(const pane of $$('#stage .pane'))restoreScroll(pane,pos);
  savePrefs();
};
$('#toc').onclick=e=>{
  const ch=e.target.closest('[data-ch]'), sec=e.target.closest('[data-sec]');
  if(ch){S.ch=ch.dataset.ch;S.history=null;S.view='auto';S.previous=false;S.filter=null;}
  if(innerWidth<=760)S.toc=false;
  if(ch && S.flow==='all'){
    topbar();renderToc();renderPanel();$('.chap[data-ch="'+q(S.ch)+'"]')?.scrollIntoView({block:'start'});
  }else if(ch){render(false);}
  if(sec){
    topbar();
    for(const section of activeSections().filter(s=>s.dataset.ch===S.ch && s.offsetWidth)){
      secBlock(section,sec.dataset.sec)?.scrollIntoView({block:'start',behavior:reducedMotion()?'instant':'smooth'});
    }
  }
  savePrefs();
};
document.addEventListener('keydown',e=>{
  if(e.isComposing || e.target.closest('input,textarea,select,dialog') || document.querySelector('dialog[open]'))return;
  if(e.key==='Escape'){
    if(editor)fold();else if(reselecting){reselecting=null;hideActions();}else if(S.panel)$('#panelClose').click();else hideActions();
    return;
  }
  if(e.altKey && e.key.toLowerCase()==='a' && selection){e.preventDefault();selectedNote();}
  if(!e.metaKey && !e.ctrlKey && !e.altKey && isCompare() && (e.key===']' || e.key==='[')){e.preventDefault();gotoChange(e.key===']'?1:-1);}
});
new ResizeObserver(adjustWidth).observe($('#stage'));
let wasSmall=innerWidth<=760;
window.addEventListener('resize',()=>{
  const small=innerWidth<=760;
  if(small && !wasSmall){S.toc=false;topbar();renderPanel();}
  wasSmall=small;hideActions();
  if(editor){
    const rect=editorBox.getBoundingClientRect();
    placeEditor(rect.left,rect.top);
  }
});
window.addEventListener('focus',()=>reload());
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')reload();});
window.addEventListener('pagehide',()=>{if(editor?.dirty)stash(editor);savePrefs();});
reload(true);
