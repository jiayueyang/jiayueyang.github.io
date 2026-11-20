import { setDocument, closeDocument, openEquation, openTutor, readFrom, setLanguage, isEngaged, closeTutor, getActivePaperId } from './player.js';

const $ = (sel, parent = document) => parent.querySelector(sel);
const $$ = (sel, parent = document) => [...parent.querySelectorAll(sel)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const get = (k, fallback) => { try { return JSON.parse(localStorage.getItem('pt.'+k)) ?? fallback; } catch { return fallback; } };
const save = (k, v) => { try { localStorage.setItem('pt.'+k, JSON.stringify(v)); } catch {} };
const defaults = {
  'entanglement entropy':'纠缠熵','Hawking radiation':'霍金辐射','event horizon':'事件视界','cosmological horizon':'宇宙学视界',
  'quantum field theory':'量子场论','general relativity':'广义相对论','renormalization group':'重整化群','critical exponent':'临界指数'
};
const state = {
  locale:get('locale','zh'), theme:get('theme','light'), prefs:get('prefs',{mode:'accurate',glossary:defaults,translate_refs:false,acks:true}),
  config:null, papers:[], listError:'', doc:null, route:'desk', ticket:0, formTab:'source', mode:'bilingual', sync:true,
  focus:null, search:'', searchHits:[], hit:0, libSearch:'', sort:get('sort','recent'), size:get('font',18), poll:null, dispose:()=>{},
  pendingFile:null, savedSource:'', readSave:null, busy:false,playerPaperId:null,requestedParagraph:null
};
const tr=(zh,en)=>state.locale==='en'?en:zh;
const paperTitle=d=>(state.locale==='zh'&&d?.title_zh)||d?.title||d?.identifier||tr('正在读取文稿','Importing document');
const methodText=k=>({pdf:tr('PDF 排版提取','PDF extraction'),latex:tr('LaTeX 源文件','LaTeX source'),sample:tr('双语阅读示例','Bilingual reading sample')})[k]||k||'';
const pathId=id=>encodeURIComponent(id);
const cleanDate=value=>{if(!value)return '';const date=new Date(typeof value==='number'&&value<1e11?value*1000:value);return Number.isNaN(date.getTime())?'':new Intl.DateTimeFormat(state.locale==='en'?'en-CA':'zh-CN',{year:'numeric',month:'short',day:'numeric'}).format(date)};
const active=s=>['queued','processing','translating'].includes(s);
const statusText=s=>({queued:tr('等待处理','Queued'),processing:tr('读取文稿','Importing'),translating:tr('正在翻译','Translating'),ready:tr('已翻译','Translated'),failed:tr('处理失败','Failed'),cancelled:tr('已取消','Cancelled')})[s]||String(s||'');
const icons={
  book:'<path d="M3 4.5c3-.8 6-.6 9 1.5 3-2.1 6-2.3 9-1.5V19c-3-.8-6-.4-9 1.8-3-2.2-6-2.6-9-1.8z"/><path d="M12 6v14.8"/>',
  desk:'<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
  library:'<path d="M4 4h3v16H4zM10 4h3v16h-3zM15.8 5l2.7-.8 4.3 15-2.7.8z"/>',
  moon:'<path d="M20.8 13a8.5 8.5 0 0 1-9.8-9.8A8.7 8.7 0 1 0 20.8 13z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M19 5l-1.5 1.5m-11 11L5 19"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  settings:'<path d="M3 6h5m4 0h9M3 12h10m4 0h4M3 18h3m4 0h11"/><circle cx="10" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
  plus:'<path d="M12 4v16M4 12h16"/>',
  link:'<path d="m10 13 4-4M8.7 15.3l-1.4 1.4a3.5 3.5 0 0 1-5-5l4-4a3.5 3.5 0 0 1 5 0m1 8.6a3.5 3.5 0 0 0 5 0l4-4a3.5 3.5 0 0 0-5-5l-1.4 1.4"/>',
  file:'<path d="M5 3h9l5 5v13H5zM14 3v6h5M8.5 13h7m-7 4h5"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  translate:'<path d="M3 5h11M8.5 3v2m3 0c-.7 5-3.3 8.7-8 11m2.6-8.2c.8 2.2 2.7 4.7 5.4 6.3M13 21l4.1-10L21 21m-6.8-3h5.4"/>',
  bookmark:'<path d="M6 3h12v18l-6-4-6 4z"/>',
  sound:'<path d="M3 9h4l5-4v14l-5-4H3zM16 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
  download:'<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  search:'<circle cx="10" cy="10" r="6.5"/><path d="m15 15 5.5 5.5"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
  back:'<path d="m14 5-7 7 7 7"/>',close:'<path d="m6 6 12 12M18 6 6 18"/>',next:'<path d="m9 5 7 7-7 7"/>',
  spark:'<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4z"/>',
  note:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 3v18m3-13h5m-5 4h5"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.3"/>'
};
const svg=k=>'<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">'+(icons[k]||'')+'</svg>';
const btn=(action,icon,label,cls='plain',extra='')=>'<button type="button" class="'+cls+'" data-action="'+action+'" '+extra+'>'+svg(icon)+esc(label)+'</button>';
const iconBtn=(action,icon,label,extra='')=>'<button type="button" class="icon-btn" data-action="'+action+'" aria-label="'+esc(label)+'" title="'+esc(label)+'" '+extra+'>'+svg(icon)+'</button>';
let playerLocale=null;
function chrome(){
  document.documentElement.lang=state.locale==='zh'?'zh-CN':'en';
  document.documentElement.dataset.theme=state.theme;
  document.documentElement.style.setProperty('--reader-size',state.size+'px');
  if(playerLocale!==state.locale){setLanguage(state.locale);playerLocale=state.locale;}
  $('#site-header').innerHTML='<div class="shell topbar"><a href="#/" class="brand" aria-label="PaperTranslate"><span class="brand-mark">'+svg('book')+'</span><span class="brand-name">PaperTranslate</span></a><nav class="topnav" aria-label="'+tr('主导航','Main navigation')+'">'+
    '<a class="nav-link '+(state.route==='desk'?'active':'')+'" '+(state.route==='desk'?'aria-current="page"':'')+' href="#/">'+svg('desk')+tr('工作台','Workspace')+'</a>'+
    '<a class="nav-link '+(state.route==='library'?'active':'')+'" '+(state.route==='library'?'aria-current="page"':'')+' href="#/library">'+svg('library')+tr('论文库','Library')+'</a></nav><div class="header-actions">'+
    '<button type="button" data-action="locale" class="tiny-btn" aria-label="'+tr('Switch to English','切换至中文')+'">'+svg('globe')+(state.locale==='zh'?'EN':'中文')+'</button><i class="header-divider"></i>'+
    iconBtn('theme',state.theme==='dark'?'sun':'moon',tr('切换明暗主题','Toggle color theme'))+iconBtn('settings','settings',tr('翻译设置','Translation settings'))+'</div></div>';
  $('#site-footer').innerHTML='<div class="shell footer-inner"><span>PaperTranslate</span>'+(state.route==='desk'?'<a href="https://svs.gsfc.nasa.gov/13326/" target="_blank" rel="noopener noreferrer">'+tr('封面图','Cover image')+' · NASA’s Goddard Space Flight Center / Jeremy Schnittman</a>':'')+'</div>';
  document.title=((state.doc?paperTitle(state.doc):'')||tr(state.route==='library'?'论文库':'研究工作台',state.route==='library'?'Library':'Workspace'))+' · PaperTranslate';
}
let toastTimer;
function notify(text){clearTimeout(toastTimer);const box=$('#toast');box.textContent=text;box.hidden=false;toastTimer=setTimeout(()=>box.hidden=true,4800)}
async function api(path,options={}){
  const response=await fetch(path,{credentials:'same-origin',...options});
  const data=await response.json().catch(()=>null);
  if(!response.ok||data===null){
    if(response.status===401)login();
    throw Error(data?.detail||data?.error||tr('服务暂时无法连接，请检查后台服务。','Could not reach the application server.'));
  }
  return data;
}
const paperURL=id=>'/api/papers/'+pathId(id);
const goPaper=id=>{location.hash='/paper/'+pathId(id)};
function modal(title,body,actions=''){
  const box=$('#dialog'); if(box.open)box.close();
  box.innerHTML='<div class="dialog-head"><h2 id="dialog-title">'+esc(title)+'</h2>'+iconBtn('dismiss','close',tr('关闭','Close'))+'</div><div class="dialog-body">'+body+'</div>'+ (actions?'<div class="dialog-actions">'+actions+'</div>':'');
  box.setAttribute('aria-labelledby','dialog-title');box.showModal();return box;
}
function login(){
  if($('#login-token')&&$('#dialog').open)return;
  const box=modal(tr('解锁论文工作台','Unlock workspace'),'<label>'+tr('工作台访问令牌','Workspace access token')+'<input id="login-token" type="password" autocomplete="current-password"></label><p role="alert" id="login-error"></p>',btn('login','book',tr('进入工作台','Unlock workspace'),'solid'));
  $('#login-token',box).onkeydown=e=>{if(e.key==='Enter')$('[data-action=login]',box).click()};
}
function settings(){
  const p=state.prefs;
  const body='<label>'+tr('翻译风格','Translation style')+'<select id="style-choice">'+[
    ['accurate','准确学术翻译','Accurate academic'],['natural','自然学术中文','Natural academic Chinese'],['literal','贴近原文','Close to original']
  ].map(([k,z,e])=>'<option value="'+k+'" '+(p.mode===k?'selected':'')+'>'+tr(z,e)+'</option>').join('')+'</select></label>'+
  '<label>'+tr('术语表','Terminology glossary')+'<textarea id="glossary" rows="7" spellcheck="false">'+esc(Object.entries(p.glossary||{}).map(([k,v])=>k+' = '+v).join('\n'))+'</textarea></label><p class="secondary">'+tr('每行填写一个对应词，例如 event horizon = 事件视界。','Use one entry per line, for example event horizon = 事件视界.')+'</p>'+
  '<label class="check"><input id="ref-pref" type="checkbox" '+(p.translate_refs?'checked':'')+'>'+tr('翻译参考文献标题','Translate reference titles')+'</label><label class="check"><input id="ack-pref" type="checkbox" '+(p.acks?'checked':'')+'>'+tr('翻译致谢','Translate acknowledgments')+'</label><p id="prefs-alert" role="alert"></p>';
  modal(tr('翻译设置','Translation settings'),body,btn('save-settings','settings',tr('保存设置','Save settings'),'solid'));
}
function saveSettings(){
  const entries=Object.create(null);
  for(const row of $('#glossary').value.split('\n').map(v=>v.trim()).filter(Boolean)){
    const pos=row.indexOf('=');
    if(pos<1||!row.slice(pos+1).trim()){ $('#prefs-alert').textContent=tr('请按「英文 = 中文」填写每一行。','Use “English = Chinese” on every line.');return;}
    entries[row.slice(0,pos).trim()]=row.slice(pos+1).trim();
  }
  state.prefs={mode:$('#style-choice').value,glossary:entries,translate_refs:$('#ref-pref').checked,acks:$('#ack-pref').checked};save('prefs',state.prefs);
  $('#dialog').close();notify(tr('翻译设置已保存','Translation settings saved'));
}
function paperList(papers,recent=false){
  if(state.listError)return '<div class="empty-state"><div class="empty-icon">'+svg('info')+'</div><div><strong>'+tr('暂时无法读取论文库','Library unavailable')+'</strong><span class="secondary">'+esc(state.listError)+'</span></div>'+btn('refresh','library',tr('重试','Retry'),'ghost')+'</div>';
  if(!papers.length)return '<div class="empty-state"><div class="empty-icon">'+svg('library')+'</div><div><strong>'+(state.libSearch&&!recent?tr('未找到匹配的论文','No matching papers'):tr('还没有导入论文','No papers imported'))+'</strong><span class="secondary">'+(state.libSearch&&!recent?tr('试试论文标题、作者或标识符。','Try a title, author, or identifier.'):tr('导入第一篇论文后，可以在这里继续阅读。','Your imported papers will be available here.'))+'</span></div></div>';
  return papers.map(p=>{
    const r=get('read.'+p.id,{});let cp=null;try{cp=localStorage.getItem('pt-listen-'+p.id)}catch{}
    return '<article class="paper-row"><div class="paper-icon">'+svg('file')+'</div><div class="paper-info"><a class="paper-title" href="#/paper/'+pathId(p.id)+'">'+esc(p.title||p.identifier||tr('正在导入的论文','Paper import'))+'</a>'+
      '<div class="paper-byline">'+esc([p.authors,p.identifier].filter(Boolean).join(' · '))+'</div><div class="paper-meta"><span class="badge '+(p.status==='failed'?'bad':'')+'">'+esc(statusText(p.status))+'</span><span>'+esc(cleanDate(p.created_at||p.created))+'</span><span>'+tr('已读 ','Read ')+(r.percent||0)+'%</span>'+(recent?'':'<span>'+tr('朗读 ','Audio ')+(p.audio_status==='ready'?tr('可下载','ready to download'):p.audio_status==='processing'||p.audio_status==='queued'?tr('正在生成','generating'):cp===null?tr('未播放','not played'):(parseInt(cp)||0)+'%')+'</span>')+'</div></div>'+
      '<div class="row-actions">'+(p.status==='ready'?iconBtn('exports','download',tr('下载文件','Downloads'),'data-id="'+esc(p.id)+'"'):'')+(!recent&&!state.config?.offline?iconBtn('delete','trash',tr('删除论文','Delete paper'),'data-id="'+esc(p.id)+'"'):'')+'</div></article>';
  }).join('');
}
const newestFirst=(a,b)=>new Date(b.created_at||b.created||0)-new Date(a.created_at||a.created||0);
function renderDesk(){
  const source=state.formTab==='source';
  $('#main').innerHTML='<div class="shell"><section class="desk-hero"><h1>'+tr('跨越语言边界，读懂研究论文','Read Research Without Language Barriers')+'</h1><p class="intro">'+tr('用 AI 翻译、聆听并理解学术论文。','Translate, listen to, and understand academic papers with AI.')+'</p></section>'+
    '<div class="work-grid"><form id="import-form" class="card import-card"><div class="section-title">'+svg('file')+'<h2>'+tr('导入论文','Import a paper')+'</h2></div><div class="segment-tabs" aria-label="'+tr('导入方式','Import method')+'">'+
    btn('source-tab','link',tr('论文链接','Paper link'),source?'selected':'','aria-pressed="'+source+'"')+
    btn('file-tab','upload',tr('上传 PDF','Upload PDF'),source?'':'selected','aria-pressed="'+!source+'"')+'</div>'+
    (source?'<div class="input-line">'+svg('link')+'<input id="source" aria-label="'+tr('论文链接或标识符','Paper link or identifier')+'" autocomplete="off" spellcheck="false" placeholder="'+tr('粘贴 arXiv 链接、DOI 或公开 PDF 地址','Paste an arXiv link, DOI, or public PDF URL')+'" value="'+esc(state.savedSource)+'"></div><p class="field-hint">arXiv · DOI · PDF URL</p>':
    '<label class="drop" id="drop">'+svg('upload')+'<span>'+tr('拖入 PDF 或选择文件','Drop a PDF or choose a file')+'</span><input aria-label="PDF" type="file" accept="application/pdf,.pdf" id="upload-file"></label><p id="file-name" class="field-hint">'+esc(state.pendingFile?.name||tr('请选择可以提取文字的 PDF。','Choose a PDF with extractable text.'))+'</p>')+
    (state.config?.offline?'<p class="static-note">'+tr('当前为公开阅读示例。','Public reading sample. ' )+'<button type="button" data-action="open-full">'+tr('启动完整工作台以导入论文','Open the full workspace to import papers')+'</button></p>':state.config?.ai_available===false?'<p class="static-note">'+tr('尚未设置服务器的 OpenAI API 密钥。你可以先阅读右侧的双语示例。','The server has no OpenAI API key configured. You can read the bilingual sample now.')+'</p>':'')+'<p role="alert" id="import-error"></p><div class="import-bottom">'+btn('settings','settings',tr('翻译设置','Translation settings'),'plain')+'<button id="submit-import" class="solid" type="submit">'+svg('translate')+tr('翻译为中文','Translate to Chinese')+'</button></div></form>'+
    '<article class="sample-card"><img class="sample-art" src="./black-hole.jpg" alt=""><span class="sample-tag">'+svg('book')+tr('双语阅读示例','Bilingual reading sample')+'</span><div class="sample-text"><h2>'+tr('黑洞的霍金温度','The Hawking temperature')+'</h2><p>'+tr('从表面引力到局域温度','From surface gravity to local temperature')+'</p><a class="button sample-open" href="#/paper/sample">'+svg('book')+(get('read.sample',{}).paragraph?tr('继续阅读','Continue reading'):tr('打开阅读器','Open reader'))+'</a></div></article></div>'+
    '<section class="recent"><div class="section-head"><div class="section-title">'+svg('library')+'<h2>'+tr('最近文稿','Recent papers')+'</h2></div><a class="button plain" href="#/library">'+tr('查看论文库','View library')+'</a></div><div class="library-frame" id="recent-list">'+paperList([...state.papers].sort(newestFirst).slice(0,4),true)+'</div></section></div>';
  const form=$('#import-form');form.addEventListener('submit',submitImport);
  if(source)$('#source').oninput=e=>state.savedSource=e.target.value;
  else{
    $('#upload-file').onchange=e=>fileChanged(e.target.files[0]);
    const zone=$('#drop');
    for(const name of ['dragover','dragenter'])zone.addEventListener(name,e=>{e.preventDefault();zone.classList.add('dragging')});
    zone.ondragleave=()=>zone.classList.remove('dragging');
    zone.ondrop=e=>{e.preventDefault();zone.classList.remove('dragging');fileChanged(e.dataTransfer.files[0])};
  }
}
function fileChanged(f){state.pendingFile=f||null;if($('#file-name'))$('#file-name').textContent=f?f.name:'PDF'}
function sendForm(data,onProgress){
  return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/papers');xhr.responseType='json';xhr.timeout=180000;
    xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(Math.round(e.loaded/e.total*100))};
    xhr.onload=()=>{if(xhr.status>=200&&xhr.status<300&&xhr.response?.id)resolve(xhr.response);else{if(xhr.status===401)login();reject(Error(xhr.response?.detail||tr('提交失败，请检查后台服务。','Import failed. Check the server.')))}};
    xhr.onerror=xhr.ontimeout=()=>reject(Error(tr('上传中断，请检查网络后重试。','Upload interrupted. Check your connection and retry.')));
    xhr.send(data);
  });
}
async function submitImport(event){
  event.preventDefault();if(state.busy)return;
  const fd=new FormData(),err=$('#import-error'),submit=$('#submit-import');
  err.textContent='';
  if(state.config?.offline){offlineHelp();return;}
  if(state.formTab==='source'){
    const value=state.savedSource.trim();if(!value){err.textContent=tr('先输入论文地址、arXiv 编号或 DOI。','Enter a paper URL, arXiv ID, or DOI.');$('#source').focus();return;}fd.set('source',value);
  }else{const file=state.pendingFile;
    if(!file||!file.name.toLowerCase().endsWith('.pdf')){err.textContent=tr('请选择 PDF 文件。','Choose a PDF file.');return;}
    if(file.size>(state.config?.max_upload_bytes||32*1024*1024)){err.textContent=tr('文件超过上传大小限制。','This file exceeds the upload size limit.');return;}fd.set('file',file);
  }
  fd.set('glossary',JSON.stringify(state.prefs.glossary));fd.set('mode',state.prefs.mode);fd.set('translate_refs',String(!!state.prefs.translate_refs));fd.set('acknowledgments',String(!!state.prefs.acks));
  state.busy=true;submit.disabled=true;
  try{
    submit.textContent=tr('正在提交…','Submitting…');
    const obj=await sendForm(fd,pc=>{if(state.formTab==='file')submit.textContent=tr('已上传 ','Uploaded ')+pc+'%'});
    state.pendingFile=null;state.savedSource='';goPaper(obj.id);
  }catch(e){err.textContent=e.message}finally{state.busy=false;submit.disabled=false;submit.textContent=tr('翻译为中文','Translate to Chinese')}
}
function libraryRows(){
  const term=state.libSearch.trim().toLowerCase();
  const papers=state.papers.filter(p=>[p.title,p.authors,p.identifier].some(v=>String(v||'').toLowerCase().includes(term)));
  papers.sort(state.sort==='title'?((a,b)=>(a.title||'').localeCompare(b.title||'')):state.sort==='oldest'?((a,b)=>newestFirst(b,a)):newestFirst);
  return paperList(papers);
}
function renderLibrary(){
  $('#main').innerHTML='<div class="shell library-page"><div class="library-heading"><h1>'+tr('我的论文库','Paper library')+'</h1><a class="button solid" href="#/">'+svg('plus')+tr('导入论文','Import a paper')+'</a></div><div class="library-tools"><div class="input-line">'+svg('search')+'<input id="library-search" type="search" placeholder="'+tr('搜索标题、作者或编号','Search title, author, or identifier')+'" aria-label="'+tr('搜索论文库','Search library')+'" value="'+esc(state.libSearch)+'"></div><select aria-label="'+tr('排序','Sort papers')+'" id="library-sort">'+[['recent','最近导入','Newest first'],['oldest','最早导入','Oldest first'],['title','标题','By title']].map(([k,z,e])=>'<option '+(state.sort===k?'selected':'')+' value="'+k+'">'+tr(z,e)+'</option>').join('')+'</select></div><div class="library-frame" id="library-rows">'+libraryRows()+'</div></div>';
  $('#library-search').oninput=e=>{state.libSearch=e.target.value;$('#library-rows').innerHTML=libraryRows()};
  $('#library-sort').onchange=e=>{state.sort=e.target.value;save('sort',state.sort);$('#library-rows').innerHTML=libraryRows()};
}
function readState(){return state.doc?get('read.'+state.doc.id,{}):{}}
let readingSyncTimer;
const pendingReading = new Map();
function syncReading(){
  clearTimeout(readingSyncTimer);
  for(const [id,record] of pendingReading){
    pendingReading.delete(id);
    fetch(paperURL(id)+'/reading-state',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(record),keepalive:true}).catch(()=>{});
  }
}
window.addEventListener('pagehide',syncReading);
function setRead(changes){
  if(!state.doc)return;
  const r={...readState(),...changes};save('read.'+state.doc.id,r);
  if(!state.config?.offline){pendingReading.set(state.doc.id,r);clearTimeout(readingSyncTimer);readingSyncTimer=setTimeout(syncReading,1000)}
  save('lastPaper',state.doc.id);
}
function paragraphs(){return state.allParagraphs||[]}
function selector(k,v){return '['+k+'="'+CSS.escape(String(v))+'"]'}
function sectionTitle(s,lang){
  const heading=s.paragraphs?.find(p=>p.kind==='heading');
  const fromText=heading?.sentences?.map(x=>x[lang]).filter(Boolean).join(' ');
  if(fromText)return fromText.replace(/\{\{eq:[^}]+\}\}/g,'…');
  if(s.title==='文首')return lang==='en'?'Front matter':'文首';
  const special={Abstract:'摘要',References:'参考文献',Acknowledgments:'致谢'};
  return (lang==='zh'&&(s.title_zh||special[s.title]))||s.title||tr('正文','Text');
}
function asset(relative){
  const name=String(relative||'').replace(/^assets\//,'');
  if(!name||name.includes('/')||name.includes('\\')||name.includes('..'))return '';
  return state.config?.offline?'./example/assets/'+pathId(name):paperURL(state.doc.id)+'/assets/'+pathId(name);
}
function math(el,tex,display=false){
  const clean=String(tex||'').trim();el.textContent=clean;
  if(!clean||!globalThis.katex)return;
  try{globalThis.katex.render(clean,el,{displayMode:!!display,output:'mathml',trust:false,strict:'ignore',maxExpand:600,maxSize:20,throwOnError:true})}catch{el.textContent=clean}
}
function annotated(text){
  const map=state.index;
  let html='',prev=0;
  // Document-controlled strings are escaped before adding interactive controls.
  const token=/\{\{eq:([\w.:-]+)\}\}|\\((?:[Cc]ite\w*|[Cc]ref|[Rr]ef|[Ee]qref|[Aa]utoref))\*?(?:\[[^\]]*\])*\{([^{}]+)\}/g;
  for(const m of String(text||'').matchAll(token)){
    html+=esc(text.slice(prev,m.index));
    if(m[1]){
      const eq=state.doc.equations?.[m[1]];
      html+=eq?'<button type="button" class="equation" data-eq="'+esc(m[1])+'" aria-label="'+tr('公式操作','Equation actions')+' '+esc(eq.label||eq.latex||m[1])+'">'+(eq.image?'<img class="figure-img" loading="lazy" src="'+esc(asset(eq.image))+'" alt="'+tr('原文公式图像','Original equation image')+'">':'<span data-math="'+esc(m[1])+'"></span>')+(eq.display&&eq.number!=null&&!/\\tag\b/.test(eq.render_latex||eq.latex||'')?' <small class="equation-number">('+esc(eq.number)+')</small>':'')+'</button>':esc(m[0]);
    }else{
      const targets=m[3].split(',').map(k=>k.trim());
      if(!targets.some(key=>map.has(key))){html+=esc(m[0]);prev=m.index+m[0].length;continue;}
      html+=targets.map(key=>{
        const target=map.get(key);return target?'<button type="button" class="cite-link" data-pgoto="'+esc(target)+'" title="'+tr('跳转至来源','Go to source')+'">['+esc(key)+']</button>':esc(key);
      }).join(', ');
    }
    prev=m.index+m[0].length;
  }
  return html+esc(String(text||'').slice(prev));
}
function refreshIndex(){
  state.index=new Map(Object.entries(state.doc.anchors||{}));
  state.allParagraphs=(state.doc.sections||[]).flatMap(s=>s.paragraphs||[]);
  state.paragraphOrder=new Map(state.allParagraphs.map((p,i)=>[p.id,i]));
  for(const p of paragraphs())if(p.citation_key)state.index.set(p.citation_key,p.id);
  for(const eq of Object.values(state.doc.equations||{}))if(eq.label&&eq.paragraph_id)state.index.set(eq.label,eq.paragraph_id);
}
function originalSource(page){
  const base=state.config?.offline?'./example/original.pdf':paperURL(state.doc.id)+'/export/original?inline=true';
  return Number.isInteger(page)&&page>0?base+'#page='+page:base;
}
function identifierLink(id){
  if(!id)return '';
  const raw=String(id).trim();
  let url='',caption=raw;
  if(/^(?:arxiv:\s*)?(?:\d{4}\.\d{4,5}|[a-z.-]+\/\d{7})(?:v\d+)?$/i.test(raw)) {
    const number=raw.replace(/^arxiv:\s*/i,'');url='https://arxiv.org/abs/'+number;caption='arXiv:'+number;
  }else if(/^10\.\d{4,9}\/[^\s]+$/i.test(raw)) {
    url='https://doi.org/'+raw;caption='DOI '+raw;
  }else{
    try{const u=new URL(raw);if(['http:','https:'].includes(u.protocol)&&!u.username&&!u.password){url=u.href;caption=u.hostname}}catch{}
  }
  return url?'<a class="metadata-link" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(caption)+'</a>':'<span>'+esc(caption)+'</span>';
}
function passage(p,lang){
  const bookmarks=state.bookmarks||[];
  const pageLink=p.page?'<a class="page-source" href="'+esc(originalSource(p.page))+'" target="_blank" rel="noopener noreferrer">'+tr('原文第 ','PDF p. ')+esc(p.page)+tr(' 页','')+'</a>':'';
  const image=p.image?'<img loading="lazy" class="figure-img" alt="'+(p.kind==='page_image'?tr('原始 PDF 页','Original PDF page'):tr('原文插图','Original figure'))+'" src="'+esc(asset(p.image))+'">':'';
  const sentences=(p.sentences||[]).map(s=>s[lang]?'<span tabindex="0" data-lang="'+lang+'" data-sentence="'+esc(s.id)+'" title="'+tr('从这句话开始朗读','Read from this sentence')+'">'+annotated(s[lang])+'</span>':'').filter(Boolean).join(lang==='en'?' ':'');
  const pending=!sentences&&!image&&p.sentences?.length?'<span class="pending-text">'+tr('等待此段译文','Translation pending')+'</span>':'';
  return '<div class="passage '+(bookmarks.includes(p.id)?'bookmarked':'')+'" data-paragraph="'+esc(p.id)+'" data-kind="'+esc(p.kind)+'">'+image+sentences+pending+pageLink+'</div>';
}
function sectionPassages(list,lang){
  const out=[];let i=0;
  while(i<list.length){
    const first=list[i];
    if(!first.table_id){out.push(passage(first,lang));i++;continue}
    const rows=new Map();
    while(i<list.length&&list[i].table_id===first.table_id){
      const p=list[i++],key=Number(p.table_row)||0,col=Number(p.table_col)||0;
      if(!rows.has(key))rows.set(key,new Map());
      const row=rows.get(key);row.set(col,(row.get(col)||'')+passage(p,lang));
    }
    const table=[...rows.entries()].sort((a,b)=>a[0]-b[0]).map(([,r])=>'<tr>'+[...r.entries()].sort((a,b)=>a[0]-b[0]).map(([,content])=>'<td>'+content+'</td>').join('')+'</tr>').join('');
    out.push('<div class="table-scroll"><table><tbody>'+table+'</tbody></table></div>');
  }
  return out.join('');
}
function pane(lang){
  return '<div class="reader-column"><div class="col-head">'+(lang==='zh'?tr('中文译文','Chinese translation'):tr('英文原文','English original'))+'</div><div tabindex="0" class="reader-pane" data-lang="'+lang+'" aria-label="'+(lang==='zh'?tr('中文阅读区','Chinese reading pane'):tr('英文阅读区','English reading pane'))+'">'+
    (state.doc.sections||[]).map(s=>'<section data-section="'+esc(s.id)+'">'+(s.paragraphs?.[0]?.kind==='heading'?'':'<h2>'+esc(sectionTitle(s,lang))+'</h2>')+sectionPassages(s.paragraphs||[],lang)+'</section>').join('')+'</div></div>';
}
function jobBox(d){
  if(d.status==='ready')return '';
  const progress=d.progress||{},done=Number(progress.done||0),total=Number(progress.total||0);
  return '<div class="job-box '+(d.status==='failed'?'error-box':'')+'" role="status"><strong>'+esc(statusText(d.status))+'</strong>'+
  (total?' · '+done+' / '+total+' '+tr('句','sentences')+' <progress value="'+done+'" max="'+total+'"></progress>':'')+
  (d.error?'<p>'+esc(d.error)+'</p>':'')+
  (d.status==='failed'&&!d.sections?.length?'<p class="secondary">'+tr('若链接无法访问，也可以返回工作台上传 PDF。','If the link is inaccessible, upload the PDF from the workspace.')+'</p>':'')+
  '<div class="inline-actions">'+(active(d.status)?btn('cancel','close',tr('取消任务','Cancel job'),'ghost'):btn('resume','translate',tr('继续处理','Resume job'),'ghost'))+'</div></div>';
}
function bookmarkHTML(){
  const ps=paragraphs();return state.bookmarks.length?state.bookmarks.map(id=>{
    const p=ps.find(x=>x.id===id);if(!p)return '';const first=p.sentences?.find(s=>s.zh||s.en);
    return '<button type="button" data-pgoto="'+esc(id)+'">'+esc((first?.[state.locale]||first?.en||tr('图片','Figure')).replace(/\{\{eq:[^}]+\}\}/g,tr('公式','Equation')).slice(0,40))+'</button>';
  }).join(''):'<p class="secondary">'+tr('尚无书签','No bookmarks yet')+'</p>';
}
function renderReader(){
  const d=state.doc;if(!d)return;
  state.dispose();state.bookmarks=Array.isArray(readState().bookmarks)?readState().bookmarks:[];refreshIndex();
  const cols=state.mode==='bilingual'?['en','zh']:[state.mode];
  const t=tr('论文内搜索','Search paper'),n=d.sections?.length||0;
  $('#main').innerHTML='<div class="shell reader"><header class="reader-top"><a class="back" href="#/library">'+svg('back')+tr('论文库','Library')+'</a><h1 class="reader-title">'+esc(paperTitle(d))+'</h1>'+
    '<div class="reader-meta">'+[d.authors?'<span>'+esc(d.authors)+'</span>':'',identifierLink(d.identifier),'<span>'+esc(statusText(d.status))+'</span>',d.method?'<span>'+esc(methodText(d.method))+'</span>':'',n?'<a class="metadata-link" href="'+esc(originalSource())+'" target="_blank" rel="noopener noreferrer">'+tr('查看原文 PDF','View original PDF')+'</a>':''].filter(Boolean).join(' · ')+'</div></header>'+
    jobBox(d)+(d.warnings?.length?'<details class="notice"><summary>'+tr('文稿处理说明','Document processing notes')+' ('+d.warnings.length+')</summary><ul>'+d.warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul></details>':'')+
    (!n?'':'<div class="reader-actions"><div class="view-toggle" aria-label="'+tr('阅读模式','Reading mode')+'">'+[['en','英文','English'],['zh','中文','Chinese'],['bilingual','双语','Bilingual']].map(([v,z,e])=>'<button type="button" data-action="view" data-value="'+v+'" aria-pressed="'+(state.mode===v)+'" class="'+(state.mode===v?'selected':'')+'">'+tr(z,e)+'</button>').join('')+'</div><div class="inline-actions">'+btn('tutor','spark',tr('AI 讲解','AI Explain'),'soft')+btn('exports','download',tr('导出','Export'),'ghost','data-id="'+esc(d.id)+'"')+'</div></div>'+
    '<div class="reader-layout"><aside class="rail-card"><details open><summary>'+tr('文章目录','Contents')+'</summary><nav class="toc">'+d.sections.map(s=>'<button type="button" data-sgoto="'+esc(s.id)+'">'+esc(sectionTitle(s,state.locale))+'</button>').join('')+'</nav></details><details><summary>'+tr('我的书签','Bookmarks')+'</summary><div class="bookmark-list">'+bookmarkHTML()+'</div></details></aside><div>'+
    '<div class="reading-tools"><div class="input-line">'+svg('search')+'<input type="search" id="paper-search" aria-label="'+t+'" placeholder="'+t+'" value="'+esc(state.search)+'"></div><span class="search-results" id="hits"></span><div class="inline-actions">'+(cols.length===2?'<label class="check"><input id="sync" type="checkbox" '+(state.sync?'checked':'')+'>'+tr('同步','Sync')+'</label>':'')+
    '<button type="button" class="tiny-btn" data-action="font-down" aria-label="'+tr('缩小字号','Decrease type size')+'">A−</button><button type="button" class="tiny-btn" data-action="font-up" aria-label="'+tr('放大字号','Increase type size')+'">A+</button>'+iconBtn('bookmark','bookmark',tr('添加或移除当前段落书签','Bookmark or unbookmark current paragraph'))+'</div></div>'+
    '<article class="reader-card"><div class="column-grid '+(cols.length===1?'single':'')+'">'+cols.map(pane).join('')+'</div></article></div></div>')+'</div>';
  for(const node of $$('[data-math]')){const eq=d.equations[node.dataset.math];math(node,eq.render_latex||eq.latex,eq.display)}
  if(!n){closeDocument();state.playerPaperId=null;return;}
  setDocument(d);state.playerPaperId=d.id;
  if($('#sync'))$('#sync').onchange=e=>{state.sync=e.target.checked;setRead({sync:state.sync});if(state.sync&&state.focus)goParagraph(state.focus)};
  $('#paper-search').oninput=e=>{state.search=e.target.value;findHits(true)};
  $('#paper-search').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();cycleHit(e.shiftKey?-1:1)}};
  attachScroll();findHits(false);
  const desired=state.focus||readState().paragraph;
  const initial=state.paragraphOrder.has(desired)?desired:paragraphs()[0]?.id;
  if(initial)goParagraph(initial,false);
}
function fetchPassage(id,el=document){return $(selector('data-paragraph',id),el)}
function goParagraph(id,highlight=true){
  if(!id)return;
  if(state.route!=='reader'&&state.playerPaperId){state.requestedParagraph={paper:state.playerPaperId,id};goPaper(state.playerPaperId);return;}
  for(const col of $$('.reader-pane')){const p=fetchPassage(id,col);if(p)scrollInside(col,p)}
  focusParagraph(id,highlight);
}
function scrollInside(pane,target){
  const to=pane.scrollTop+target.getBoundingClientRect().top-pane.getBoundingClientRect().top-5;
  if(Math.abs(to-pane.scrollTop)>1)pane.scrollTop=Math.max(0,to);
}
function focusParagraph(id,highlight){
  if(!state.doc||!id)return;
  $$('.passage.focused').forEach(x=>x.classList.remove('focused'));
  if(highlight)for(const p of $$(selector('data-paragraph',id)))p.classList.add('focused');
  state.focus=id;
  const p=fetchPassage(id);const section=p?.closest('[data-section]')?.dataset.section;
  $$('.toc button').forEach(b=>b.classList.toggle('selected',b.dataset.sgoto===section));
  const index=state.paragraphOrder.get(id)??0,count=paragraphs().length;
  const r=readState();
  const percent=count?Math.round(100*(index+1)/count):0;
  if(r.paragraph!==id||r.mode!==state.mode)setRead({paragraph:id,section,mode:state.mode,percent});
}
function attachScroll(){
  const panes=$$('.reader-pane');let frame=0,from=null;let releaseTimer;const listeners=[];
  function onScroll(p){
    if(from&&from!==p)return;from=p;
    clearTimeout(releaseTimer);releaseTimer=setTimeout(()=>from=null,100);
    if(frame)return;frame=requestAnimationFrame(()=>{
      frame=0;if(!p.isConnected)return;
      const top=p.getBoundingClientRect().top+7;
      // Determine the visible paragraph within this language pane; group alignment uses stable IDs.
      const all=p.querySelectorAll('[data-paragraph]');let lo=0,hi=all.length-1;
      while(lo<hi){const mid=(lo+hi)>>1;if(all[mid].getBoundingClientRect().bottom<top)lo=mid+1;else hi=mid}
      const first=all[lo];if(!first)return;
      const id=first.dataset.paragraph;focusParagraph(id,false);
      if(state.sync)for(const dest of panes)if(dest!==p){
        const tgt=fetchPassage(id,dest);if(tgt){
          const rect=first.getBoundingClientRect(),frac=Math.max(0,Math.min(1,(top-rect.top)/Math.max(1,rect.height)));
          const target=dest.scrollTop+tgt.getBoundingClientRect().top-dest.getBoundingClientRect().top-7+frac*tgt.getBoundingClientRect().height;
          if(Math.abs(dest.scrollTop-target)>2)dest.scrollTop=Math.max(0,target);
        }
      }
    });
  }
  for(const pane of panes){const fn=()=>onScroll(pane);pane.addEventListener('scroll',fn,{passive:true});listeners.push([pane,fn])}
  state.dispose=()=>{cancelAnimationFrame(frame);clearTimeout(releaseTimer);listeners.forEach(([el,fn])=>el.removeEventListener('scroll',fn));state.dispose=()=>{}};
}
function findHits(jump){
  const term=state.search.trim().toLowerCase();state.hit=0;
  state.searchHits=term?paragraphs().filter(p=>(p.sentences||[]).some(s=>[s.en,s.zh].some(v=>String(v||'').toLowerCase().includes(term)))).map(p=>p.id):[];
  const set=new Set(state.searchHits);
  $$('.passage').forEach(p=>p.classList.toggle('match',set.has(p.dataset.paragraph)));
  if(jump&&state.searchHits.length)goParagraph(state.searchHits[0]);
  showHits();
}
function showHits(){
  const out=$('#hits');if(!out)return;
  const total=state.searchHits.length;
  out.innerHTML=!state.search.trim()?'':!total?tr('无结果','No matches'):(state.hit+1)+' / '+total+iconBtn('prev-hit','back',tr('上一个结果','Previous match'))+iconBtn('next-hit','next',tr('下一个结果','Next match'));
}
function cycleHit(dir){if(!state.searchHits.length)return;state.hit=(state.hit+dir+state.searchHits.length)%state.searchHits.length;goParagraph(state.searchHits[state.hit]);showHits()}
function toggleBookmark(){
  const id=state.focus||paragraphs()[0]?.id;if(!id)return;const b=state.bookmarks;const add=!b.includes(id);
  state.bookmarks=add?[...b,id]:b.filter(x=>x!==id);setRead({bookmarks:state.bookmarks});$('.bookmark-list').innerHTML=bookmarkHTML();
  for(const el of $$(selector('data-paragraph',id)))el.classList.toggle('bookmarked',add);
  notify(add?tr('已为该段添加书签','Paragraph bookmarked'):tr('已移除书签','Bookmark removed'));
}
const staticFiles={original:'original.pdf',chinese:'chinese.pdf',bilingual:'bilingual.pdf',markdown:'paper.md.txt'};
const exportNames=()=>({original:tr('原文 PDF','Original PDF'),chinese:tr('完整中文 PDF','Chinese PDF'),bilingual:tr('完整双语 PDF','Bilingual PDF'),markdown:tr('中文 Markdown','Chinese Markdown'),latex:tr('翻译后的 LaTeX 工程','Translated LaTeX project')});
function showExports(id){
  const d=state.doc?.id===id?state.doc:state.papers.find(p=>p.id===id);if(!d)return;
  const choices=Object.entries(exportNames()).filter(([k])=>(k==='original'||d.status==='ready')&&(k!=='latex'||(!state.config?.offline&&/latex|source|tex/i.test(d.method||''))));
  modal(tr('导出文稿','Export document'),'<p class="export-summary">'+esc(d.title)+'</p>'+choices.map(([k,v])=>btn('download','download',v,'download-link','data-kind="'+k+'" data-id="'+esc(id)+'"')).join('')+'<p id="download-status" role="status"></p>');
}
async function download(button){
  const id=button.dataset.id,k=button.dataset.kind;const msg=$('#download-status');
  const u=state.config?.offline?'./example/'+staticFiles[k]:paperURL(id)+'/export/'+k;
  button.disabled=true;msg.textContent=tr('正在准备文件…','Preparing download…');
  try{
    const res=await fetch(u);const type=res.headers.get('Content-Type')||'';
    if(!res.ok||type.includes('text/html')||type.includes('application/json')){const error=await res.json().catch(()=>({}));throw Error(error.detail||tr('文件暂不可用。','File is currently unavailable.'))}
    const blob=await res.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;
    const suffix={original:'pdf',chinese:'pdf',bilingual:'pdf',markdown:'md',latex:'zip'}[k];
    const docTitle=(state.doc?.id===id?state.doc:state.papers.find(x=>x.id===id))?.title||'PaperTranslate';
    a.download=docTitle.replace(/[\\/:*?"<>|]/g,'_').slice(0,110)+'-'+k+'.'+suffix;
    document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    msg.textContent=tr('文件已准备好。','File ready.');
  }catch(e){msg.textContent=e.message}finally{button.disabled=false}
}
function offlineHelp(){
  modal(tr('启动完整工作台','Open the full workspace'),'<p>'+tr('当前页面提供双语阅读示例。要导入自己的论文、使用 AI 翻译或生成音频，请在 GitHub Codespaces 或本机启动服务。','This page hosts a bilingual reading sample. Start the service in GitHub Codespaces or locally to import your papers, translate them, and generate audio.')+'</p><a class="button solid" href="https://codespaces.new/jiayueyang/jiayueyang.github.io" target="_blank" rel="noopener noreferrer">'+tr('在 Codespaces 中打开','Open in Codespaces')+'</a><a class="button ghost" href="https://github.com/jiayueyang/jiayueyang.github.io/tree/main/PaperTranslate#readme" target="_blank" rel="noopener noreferrer">'+tr('查看启动说明','View setup instructions')+'</a>');
}
function renderCurrent(){
  chrome();if(state.route==='library')renderLibrary();else if(state.route==='reader'&&state.doc)renderReader();else if(state.route==='desk')renderDesk();
}
async function listPapers(){
  if(state.config?.offline){
    state.listError='';state.papers=[];
    if(get('read.sample',{}).paragraph){try{state.sampleCache??=await getDoc('sample');state.papers=[state.sampleCache]}catch(e){state.listError=e.message}}
    return;
  }
  try{const obj=await api('/api/papers');state.papers=Array.isArray(obj)?obj:obj.papers||[];state.listError='';}
  catch(e){state.listError=e.message;state.papers=[]}
}
async function getDoc(id){
  if(state.config?.offline){
    if(id!=='sample')throw Error(tr('静态预览只提供示例文稿。','Only the sample is available in static preview.'));
    const res=await fetch('./sample.json');if(!res.ok)throw Error(tr('示例文稿暂时不可用。','The sample is currently unavailable.'));return res.json();
  }
  return api(id==='sample'?'/api/sample':paperURL(id));
}
function showError(message){
  $('#main').innerHTML='<div class="shell library-page"><div class="notice" role="alert">'+esc(message)+'</div><a class="button ghost" href="#/">'+tr('返回工作台','Back to workspace')+'</a></div>';
}
function poll(id){
  clearTimeout(state.poll);if(!active(state.doc?.status))return;
  const ticket=state.ticket; const previous=state.doc;
  state.poll=setTimeout(async()=>{
    try{
      const next=await getDoc(id);if(ticket!==state.ticket)return;
      state.doc=next;
      if(next.status!==previous.status||next.progress?.done!==previous.progress?.done||next.progress?.total!==previous.progress?.total||next.updated_at!==previous.updated_at){renderCurrent()}
      poll(id);
    }catch(e){if(ticket===state.ticket){notify(e.message);poll(id)}}
  },2400);
}
async function route(){
  const ticket=++state.ticket;clearTimeout(state.poll);state.dispose();
  const old=state.doc?.id;const path=location.hash.slice(1)||'/';const isReader=path.startsWith('/paper/');
  let id;try{id=isReader?decodeURIComponent(path.substring(7)):null}catch{showError(tr('无效论文编号','Invalid paper identifier'));return}
  if(!isReader){closeTutor();if(!isEngaged()){closeDocument();state.playerPaperId=null}}
  else if(getActivePaperId()&&getActivePaperId()!==id){closeDocument();state.playerPaperId=null}
  if(old&&old!==id)state.doc=null;
  state.route=isReader?'reader':path==='/library'?'library':'desk';state.search='';state.searchHits=[];state.focus=null;
  chrome();
  if(!isReader){
    renderCurrent();await listPapers();if(ticket!==state.ticket)return;
    if(state.route==='desk'){const list=$('#recent-list');if(list)list.innerHTML=paperList([...state.papers].sort(newestFirst).slice(0,4),true)}
    else renderLibrary();
  }else{
    $('#main').innerHTML='<div class="shell library-page"><p role="status">'+tr('正在打开论文…','Opening paper…')+'</p></div>';
    try{
      const doc=await getDoc(id);if(ticket!==state.ticket)return;state.doc=doc;if(id==='sample')state.sampleCache=doc;let r=readState();
      if(!state.config?.offline&&!r.paragraph){try{const saved=await api(paperURL(id)+'/reading-state');if(saved?.paragraph){r=saved;save('read.'+id,saved)}}catch{}if(ticket!==state.ticket)return;}
      state.mode=['en','zh','bilingual'].includes(r.mode)?r.mode:'bilingual';state.sync=r.sync!==false;state.focus=(state.requestedParagraph?.paper===id&&state.requestedParagraph.id)||r.paragraph||null;state.requestedParagraph=null;
      renderCurrent();poll(id);
    }catch(e){if(ticket===state.ticket)showError(e.message)}
  }
}
document.addEventListener('click',async e=>{
  if(!(e.target instanceof Element))return;
  const el=e.target.closest('[data-action], [data-eq], [data-sgoto], [data-pgoto], [data-sentence], [data-paragraph]');
  if(!el||e.target.closest('a.page-source'))return;
  const act=el.dataset.action;
  try{
    if(el.dataset.eq){openEquation(el.dataset.eq);return}
    if(el.dataset.pgoto){goParagraph(el.dataset.pgoto);return}
    if(el.dataset.sgoto){const sec=state.doc.sections.find(s=>s.id===el.dataset.sgoto);if(sec?.paragraphs?.length)goParagraph(sec.paragraphs[0].id);return}
    if(el.dataset.sentence){
      if(String(getSelection()||'').trim())return;
      const lang=el.closest('[data-lang]')?.dataset.lang||'zh',p=el.closest('[data-paragraph]');if(p){focusParagraph(p.dataset.paragraph,true);if(state.sync)for(const col of $$('.reader-pane')){if(!col.contains(p)){const match=fetchPassage(p.dataset.paragraph,col);if(match)scrollInside(col,match)}}}
      readFrom(el.dataset.sentence,lang);return;
    }
    if(el.dataset.paragraph){goParagraph(el.dataset.paragraph);return}
    switch(act){
      case 'locale':state.locale=state.locale==='zh'?'en':'zh';save('locale',state.locale);renderCurrent();break;
      case 'theme':state.theme=state.theme==='dark'?'light':'dark';save('theme',state.theme);chrome();break;
      case 'dismiss':$('#dialog').close();break;
      case 'settings':settings();break;
      case 'open-full':offlineHelp();break;
      case 'save-settings':saveSettings();break;
      case 'login':{
        try{await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:$('#login-token').value})});$('#dialog').close();state.config=await api('/api/config');await route()}
        catch(err){if($('#login-error'))$('#login-error').textContent=err.message}
        break;
      }
      case 'refresh':await route();break;
      case 'source-tab':case 'file-tab':if(state.busy)return;state.formTab=act==='source-tab'?'source':'file';renderDesk();break;
      case 'view':state.mode=el.dataset.value;setRead({mode:state.mode});renderReader();break;
      case 'bookmark':toggleBookmark();break;
      case 'font-up':case 'font-down':state.size=Math.max(14,Math.min(32,state.size+(act==='font-up'?1:-1)));save('font',state.size);document.documentElement.style.setProperty('--reader-size',state.size+'px');break;
      case 'prev-hit':case 'next-hit':cycleHit(act==='prev-hit'?-1:1);break;
      case 'tutor':openTutor();break;
      case 'exports':showExports(el.dataset.id);break;
      case 'download':await download(el);break;
      case 'cancel':case 'resume':{
        if(!state.doc)break;const id=state.doc.id;el.disabled=true;
        try{await api(paperURL(id)+'/'+act,{method:'POST'});if(state.doc?.id===id){state.doc=await getDoc(id);renderCurrent();poll(id)}}
        finally{el.disabled=false}break;
      }
      case 'delete':{
        const paper=state.papers.find(p=>p.id===el.dataset.id);
        modal(tr('删除论文？','Delete paper?'),'<p>'+esc(paper?.title||'')+'</p><p class="secondary">'+tr('此操作会删除原文、译文和生成的音频。','This removes the source, translation, and generated recordings.')+'</p>',btn('dismiss','close',tr('保留','Keep'),'ghost')+btn('confirm-delete','trash',tr('删除','Delete'),'ghost danger-text','data-id="'+esc(el.dataset.id)+'"'));break;
      }
      case 'confirm-delete':{
        const id=el.dataset.id;el.disabled=true;
        try{
          const res=await fetch(paperURL(id),{method:'DELETE'});if(!res.ok){const x=await res.json().catch(()=>({}));throw Error(x.detail||tr('删除失败','Deletion failed'))}
          if(state.playerPaperId===id){closeDocument();state.playerPaperId=null}
          try{const keys=Object.keys(localStorage).filter(k=>k==='pt.read.'+id||['pt-listen-','pt-duration-','pt-player-','pt-narration-','pt-audio-export-'].some(pre=>k.startsWith(pre+id)));keys.forEach(k=>localStorage.removeItem(k))}catch{}
          $('#dialog').close();await route();
        }finally{el.disabled=false}break;
      }
      case 'read-selection':{const node=state.selectionStart;if(node)readFrom(node.dataset.sentence,node.closest('[data-lang]')?.dataset.lang||'zh');$('#dialog').close();break;}
    }
  }catch(err){notify(err.message)}
});
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('span[data-sentence]')){e.preventDefault();e.target.click()}});
document.addEventListener('contextmenu',e=>{
  const text=getSelection(),n=text?.anchorNode;
  if(!text?.toString().trim()||!n)return;const start=(n.nodeType===1?n:n.parentElement)?.closest('[data-sentence]');
  if(!start)return;e.preventDefault();state.selectionStart=start;modal(tr('已选文字','Selected text'),'<p>'+esc(text.toString().slice(0,260))+'</p>',btn('read-selection','sound',tr('从这里朗读','Read from here'),'solid'));
});
globalThis.PaperTranslate={goParagraph,math,locale:()=>state.locale,notify,offline:()=>!!state.config?.offline,offlineHelp};
async function init(){
  if(location.hostname==='jiayueyang.github.io'||/^\/PaperTranslate(?:\/|$)/.test(location.pathname)){state.config={offline:true,ai_available:false}}
  else{try{state.config=await api('/api/config')}catch{state.config={offline:true,ai_available:false}}}
  window.addEventListener('hashchange',route);await route();
  if(state.config.auth_required&&!state.config.authenticated)login();
}
init();
