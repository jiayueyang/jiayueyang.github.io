// Each recording contains exactly one source sentence. Only media events advance the cursor.
const get = id => document.getElementById(id);
const storage = {
  read(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Storage is optional. */ } },
  remove(key) { try { localStorage.removeItem(key); } catch {} },
};
const eqPattern = /\{\{eq:([\w.:-]+)\}\}/g;
const textOnly = value => String(value || '').replace(eqPattern, '').trim();
const labels = (zh, en) => locale === 'en' ? en : zh;
const time = seconds => Number.isFinite(seconds) ? Math.floor(seconds / 60) + ':' + String(Math.floor(seconds % 60)).padStart(2, '0') : '–';
function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
const icons = {
  play:'m8 5 11 7-11 7z', pause:'M8 5v14M16 5v14', prev:'M5 5v14m14-14L8 12l11 7z', next:'M19 5v14M5 5l11 7-11 7z',
  list:'M4 6h16M4 12h16M4 18h16', close:'m6 6 12 12M18 6 6 18', sound:'M4 10h4l5-4v12l-5-4H4zM16 9c2 2 2 4 0 6m2-9c4 3 4 9 0 12',
  settings:'M3 7h10m4 0h4M3 17h3m4 0h11M13 4v6m-7 4v6', stop:'M6 6h12v12H6z', back:'m14 6-6 6 6 6',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', repeat:'M5 9a8 8 0 1 1-1 6M5 3v6h6',
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('aria-hidden','true');
  const p = document.createElementNS(svg.namespaceURI,'path'); p.setAttribute('d',icons[name]);
  svg.append(p); return svg;
}
function button(text, fn, cls='pt-tool', symbol) {
  const b = element('button',cls); b.type = 'button'; b.title = text; b.setAttribute('aria-label', text);
  if (symbol) b.append(icon(symbol));
  else b.textContent = text;
  b.addEventListener('click', fn); return b;
}
function selection(label, entries, val, change) {
  const sel = element('select'); sel.setAttribute('aria-label',label);
  for (const [value,caption] of entries) { const opt = element('option','',caption); opt.value=value; sel.append(opt); }
  sel.value=String(val); sel.addEventListener('change',()=>change(sel.value)); return sel;
}
function labelled(title,input) { const node=element('label','pt-setting'); node.append(element('span','',title),input); return node; }
function feedback(message) {
  if (get('pt-message')) get('pt-message').textContent=message;
  if (window.PaperTranslate?.notify) window.PaperTranslate.notify(message);
}
function messageFrom(err) { return typeof err?.message === 'string' ? err.message : String(err); }
async function api(url, body, signal, method='POST') {
  const r=await fetch(url,{method,headers:body ? {'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,signal,credentials:'same-origin'});
  if (!r.ok) {
    let msg='HTTP '+r.status;
    try { const j=await r.json(); if(typeof(j.detail || j.error)==='string') msg=j.detail || j.error; } catch {}
    const err=new Error(msg);err.status=r.status;throw err;
  }
  return r;
}
let doc=null, locale='zh', mode='zh', items=[], position=0, offset=0, audio=null, clipReady=false;
let pending=false, playing=false, engaged=false, generation=0, prefetchController=null, clipController=null, drawer=null, drawerAbort=null, drawerNonce=0;
let explanation=null, literal=null, activeJob=null, playbackAlert='', optionsOpen=false, durationMap={}, lastSaved=-1;
let highlighted=[], clips=new Map(), settings=storage.read('pt-voice-settings',{});
const offeredVoices=['alloy','coral','sage','echo','shimmer'];
settings={voice:'alloy',speed:1,formula_mode:'brief',follow:true,...settings};
if(!offeredVoices.includes(settings.voice)) settings.voice='alloy';
if(!Number.isFinite(settings.speed)||settings.speed<.5||settings.speed>2.5) settings.speed=1;
if(!['brief','full','skip'].includes(settings.formula_mode)) settings.formula_mode='brief';
let deferredPosition=null, timelineCache=null, utterance=null;
const deviceMode=()=>Boolean(window.PaperTranslate?.offline?.());
const serviceMessage=()=>labels('请在连接 OpenAI 的完整应用中使用此功能。','Open the full application with OpenAI connected to use this feature.');
const rootPath=()=>'/api/papers/'+encodeURIComponent(doc.id);
const bookMode = () => mode === 'zh' || mode === 'en';
const trackKey=()=>bookMode() ? mode : mode+'-'+(explanation?.id || literal?.eqid || '');
const prefsKey=()=> 'pt-player-'+doc.id;
const recordKey=()=> 'pt-narration-'+doc.id;
const durKey=()=> 'pt-duration-'+doc.id;
const mediaLang=()=>mode==='en'||(mode==='equation'&&literal?.lang==='en')?'en':'zh';
const current=()=>items[position];
export function paperTrack(document,lang,formulaMode='brief') {
  const items=[];
  for (const section of document.sections || []) {
    for (const paragraph of section.paragraphs || []) {
      for(const s of paragraph.sentences || []) {
        if(!s.en && !s.zh) continue;
        if(formulaMode==='skip' && s[lang] && !textOnly(s[lang])) continue;
        items.push({...s,sectionId:section.id,sectionTitle:section.title,paragraphId:paragraph.id});
      }
    }
  }
  return items;
}
function buildTrack() {
  if(!doc) return [];
  if(bookMode()) return paperTrack(doc,mode,settings.formula_mode);
  if(mode==='equation') return literal ? [literal] : [];
  return (explanation?.sentences || []).map(s=>({...s,paragraphId:s.paragraph_id || s.id,sectionId:s.section_id || s.heading,sectionTitle:s.heading || ( mode==='tutorial'?labels('论文讲解','Paper tutorial'):labels('公式讲解','Equation explanation'))}));
}
function itemKey(s) { return JSON.stringify([trackKey(),s.id,mediaLang(),settings.voice,settings.formula_mode,s.override || '']); }
function clipDuration(s) { return durationMap[itemKey(s)]; }
function timingForTrack() {
  if(timelineCache?.items===items && timelineCache.voice===settings.voice && timelineCache.formula===settings.formula_mode) return timelineCache;
  const values=items.map(clipDuration), prefix=[0];
  for(const value of values) prefix.push(prefix.at(-1)+(value || 0));
  timelineCache={items,voice:settings.voice,formula:settings.formula_mode,values,prefix,all:values.length>0 && values.every(x=>x>0)};
  return timelineCache;
}
export function seekTarget(values,ratio) {
  ratio=Math.max(0,Math.min(1,ratio || 0));
  if(!values.length) return {index:0,time:0,fraction:0};
  if(values.every(d=>Number.isFinite(d)&&d>0)) {
    let t=ratio*values.reduce((a,b)=>a+b,0);
    for(let i=0;i<values.length;i++){ if(t<values[i] || i===values.length-1) return {index:i,time:t,fraction:t/values[i]}; t-=values[i]; }
  }
  const part=ratio*values.length, index=Math.min(values.length-1,Math.floor(part));
  const fraction=part-index;
  return {index,time:Number.isFinite(values[index]) ? fraction*values[index]:0,fraction};
}
export function groupJump(track,index,dir,field){
  if(!track[index]) return 0;
  if(dir>0) {
    let i=index+1; while(i<track.length && track[i][field]===track[index][field]) ++i;
    return i<track.length ? i:index;
  }
  let i=index; while(i>0 && track[i-1][field]===track[index][field]) --i;
  if(i===0) return 0;
  --i; while(i>0 && track[i-1][field]===track[i][field]) --i;
  return i;
}
function safeTime() { return clipReady && Number.isFinite(audio?.currentTime) ? audio.currentTime : offset; }
function save() {
  if (!doc || !current()) return;
  const seconds=safeTime(), key=trackKey(), state=storage.read(prefsKey(),{});
  const cursor={id:current().id,time:seconds};
  if(deferredPosition!==null) cursor.fraction=deferredPosition;
  state[key]=cursor; state.mode=mode; if(explanation?.id)state.explanation=explanation.id; storage.write(prefsKey(),state);
  if(bookMode()) {
    const fraction=clipDuration(current())?Math.min(1,seconds/clipDuration(current())):0;
    storage.write('pt-listen-'+doc.id,Math.round(100*(position+fraction)/Math.max(1,items.length)));
  }
}
function restore() {
  const saved=storage.read(prefsKey(),{})[trackKey()];
  position=Math.max(0,items.findIndex(s=>s.id===saved?.id)); offset=Math.max(0,Number(saved?.time)||0);
  deferredPosition=typeof saved?.fraction==='number'?saved.fraction:null;
}
function clearHighlights() { for (const [el,c] of highlighted) el.classList.remove(c); highlighted=[]; }
function mark(node,cls){ node.classList.add(cls); highlighted.push([node,cls]); }
function highlight(scroll=true) {
  clearHighlights(); const s=current(); if(!s) return;
  const sid=CSS.escape(String(s.id)), pid=CSS.escape(String(s.paragraphId || ''));
  let target;
  for(const node of document.querySelectorAll('[data-sentence="'+sid+'"],[data-tutor-sentence="'+sid+'"]')) {
    const lang=node.closest('[data-lang]')?.dataset.lang;
    if(!lang || lang===mediaLang()) {mark(node,'pt-speaking'); if(node.getClientRects().length) target=node;}
  }
  if(bookMode()||mode==='equation') for(const node of document.querySelectorAll('[data-paragraph="'+pid+'"]')) {
    mark(node,'pt-speaking-paragraph'); if(!target && node.getClientRects().length) target=node;
  }
  if(scroll && settings.follow && target) {
    const rect=target.getBoundingClientRect(); const zone=get('pt-audio-bar')?.getBoundingClientRect();
    const pane=target.closest('.reader-pane,.pt-drawer-body')?.getBoundingClientRect();
    const top=Math.max(0,pane?.top || 0), bottom=Math.min(zone?.top || window.innerHeight,pane?.bottom || window.innerHeight);
    if(rect.top<top+5 || rect.bottom>bottom-5)
      target.scrollIntoView({block:'center',inline:'nearest',behavior:'smooth'});
  }
}
function abortPlayback() {
  generation++; pending=false; playing=false; clipReady=false; lastSaved=-1;
  clipController?.abort(); prefetchController?.abort(); clipController=prefetchController=null;
  if(utterance){utterance.onstart=utterance.onend=utterance.onerror=null;window.speechSynthesis?.cancel();utterance=null;}
  if(audio) {audio.onplaying=audio.onended=audio.onerror=audio.ontimeupdate=audio.onloadedmetadata=audio.onpause=null;audio.pause();audio.removeAttribute('src');audio.load();audio=null;}
  clearHighlights();
}
function disposeClips() {for(const url of clips.values()) URL.revokeObjectURL(url); clips.clear();}
function saveOptions(){storage.write('pt-voice-settings',settings);}
function switchTrack(next,exp=null,eq=null) {
  save(); abortPlayback(); mode=next; explanation=exp; literal=eq;
  items=buildTrack();restore(); playbackAlert='';renderBar();
}
function playbackError(err) {
  pending=playing=false; playbackAlert=messageFrom(err);
  if(get('pt-message')) get('pt-message').textContent=playbackAlert;
  updateBar(); feedback(playbackAlert);
}
function sentenceBody(s) {
  if(bookMode() && !s[mode]) throw new Error(labels('这一句尚未完成翻译。','This sentence has not been translated yet.'));
  return {sentence_id:s.id,language:mediaLang(),voice:settings.voice,formula_mode:settings.formula_mode,
    ...(!bookMode() && mode!=='equation' ? {explanation_id:explanation.id}:{}),...(s.override ? {override:s.override}: {})};
}
async function loadClip(s,signal) {
  const k=itemKey(s);
  if(clips.has(k)) return clips.get(k);
  const response=await api(rootPath()+'/audio',sentenceBody(s),signal);
  const data=await response.blob(); if(signal.aborted) throw new DOMException('Aborted','AbortError');
  if(!data.size) throw Error(labels('音频文件为空。','The audio recording is empty.'));
  if(!clips.has(k)) {
    const url=URL.createObjectURL(data); clips.set(k,url);
    if(clips.size>8){const old=clips.keys().next().value; URL.revokeObjectURL(clips.get(old)); clips.delete(old);}
  }
  return clips.get(k);
}
function prefetchNext() {
  prefetchController?.abort(); const next=items[position+1];
  if(!next || !playing || clips.has(itemKey(next))) return;
  const ctrl=new AbortController(); prefetchController=ctrl;
  loadClip(next,ctrl.signal).catch(()=>{}); // Demand playback surfaces failures and permits a retry.
}
function deviceFormula(eqid,lang,style) {
  const eq=doc.equations?.[eqid];if(style==='skip')return '';
  const spoken=eq?.pronunciation?.[lang];if(spoken)return spoken;
  // Preserve visual math and avoid inventing a pronunciation when none was authored.
  return lang==='en' ? 'See the displayed equation.' : '请参阅此处的公式。';
}
function startDevice(s,gen) {
  if(!window.speechSynthesis || typeof SpeechSynthesisUtterance==='undefined'){
    playbackError(Error(labels('当前浏览器未提供系统语音，请使用支持朗读的浏览器。','This browser does not provide speech synthesis.')));return;
  }
  const lang=mediaLang(), raw=s.override || s['spoken_'+lang] || s[lang];
  if(!raw){playbackError(Error(labels('这一句尚未完成翻译。','This sentence is not translated yet.')));return;}
  const phrase=raw.replace(eqPattern,(_match,id)=>deviceFormula(id,lang,settings.formula_mode)).trim();
  if(!phrase){if(position+1<items.length)startAt(position+1);else{pending=false;updateBar();}return;}
  const u=new SpeechSynthesisUtterance(phrase);utterance=u;u.lang=lang==='zh'?'zh-CN':'en-US';u.rate=settings.speed;
  const voices=window.speechSynthesis.getVoices();
  u.voice=voices.find(v=>v.voiceURI===settings.deviceVoice&&v.lang.startsWith(lang)) || voices.find(v=>v.lang.startsWith(lang)&&v.localService) || voices.find(v=>v.lang.startsWith(lang)) || null;
  offset=0;deferredPosition=null;
  u.onstart=()=>{if(gen!==generation)return;pending=false;playing=true;highlight();updateBar();save();};
  u.onend=()=>{if(gen!==generation)return;utterance=null;if(position+1<items.length)startAt(position+1);else{pending=playing=false;updateBar();save();}};
  u.onerror=e=>{if(gen!==generation)return;utterance=null;playbackError(Error(labels('系统朗读失败。请检查设备已安装的语音。','Device speech failed. Check installed voices.')+' ('+e.error+')'));};
  window.speechSynthesis.speak(u);
}
async function startAt(i=position, seconds=0, fraction=null) {
  if(!doc||!items.length) return;
  abortPlayback(); position=Math.max(0,Math.min(items.length-1,i)); offset=Math.max(0,seconds || 0);deferredPosition=fraction;
  engaged=true; pending=true; playbackAlert=''; const gen=generation, s=current(); updateBar();
  if(deviceMode()){startDevice(s,gen);return;}
  const ctrl=new AbortController(); clipController=ctrl;
  try {
    const url=await loadClip(s,ctrl.signal); if(gen!==generation) return;
    const player=new Audio(); audio=player; player.preload='auto';player.playbackRate=settings.speed;
    player.onloadedmetadata=()=>{
      if(gen!==generation)return;
      if(Number.isFinite(player.duration) && player.duration>0) {
        durationMap[itemKey(s)]=player.duration;timelineCache=null;storage.write(durKey(),durationMap);
        offset=Math.max(0,Math.min(player.duration-.002,fraction===null?offset:player.duration*fraction));
        player.currentTime=offset;
      }
      deferredPosition=null; clipReady=true;updateBar();
    };
    player.onplaying=()=>{if(gen!==generation)return; pending=false; playing=true;highlight();updateBar();save();prefetchNext();};
    player.ontimeupdate=()=>{
      if(gen!==generation || !clipReady) return;
      offset=player.currentTime;updateBar();
      if(Math.floor(offset)!==lastSaved){lastSaved=Math.floor(offset);save();}
    };
    player.onended=()=>{
      if(gen!==generation)return;
      if(position+1<items.length) {startAt(position+1); return;}
      offset=player.duration || 0;pending=playing=false;save();updateBar();
    };
    player.onerror=()=>{if(gen===generation) playbackError(new Error(labels('这段音频无法播放，可点击重试。','This clip could not be played. Press play to retry.')));};
    player.src=url; await player.play();
  } catch(err) {if(gen===generation && err.name!=='AbortError') playbackError(err);}
}
function pause() {
  if(pending) {offset=safeTime();abortPlayback();} 
  else {if(utterance)window.speechSynthesis?.pause();audio?.pause(); playing=false; prefetchController?.abort();}
  save(); updateBar();
}
function toggle() {
  if(pending||playing){pause();return;}
  if(utterance && deviceMode()){window.speechSynthesis.resume();playing=true;highlight(false);updateBar();return;}
  if(audio && clipReady && !audio.ended && !audio.error){
    audio.playbackRate=settings.speed;audio.play().catch(playbackError); return;
  }
  startAt(position,audio?.ended?0:offset,deferredPosition);
}
function seek(i,seconds=0,fraction=null,autoplay=playing||pending) {
  if(!items.length) return;
  if(autoplay){startAt(i,seconds,fraction);return;}
  abortPlayback();position=Math.max(0,Math.min(items.length-1,i));offset=seconds;deferredPosition=fraction;
  highlight();save();updateBar();
}
export function getActivePaperId() { return doc?.id ?? null; }
export function isEngaged() { return Boolean(doc && engaged); }
export function closeTutor() { closeDrawer(); }
export function readFrom(sentenceId,lang='zh') {
  if(!doc) return;
  if(mode!==lang) switchTrack(lang);
  const i=items.findIndex(s=>s.id===sentenceId);
  if(i>=0) startAt(i);
  else feedback(labels('当前朗读设置已跳过这句公式。','The formula reading preference skips this sentence.'));
}
function sliderValue() {
  if(deviceMode())return {ratio:position/Math.max(1,items.length),all:false,total:0,elapsed:0};
  const {values:d,all,prefix}=timingForTrack(), t=safeTime();
  const segFraction=deferredPosition ?? ((d[position] || 0)>0?t/d[position]:0);
  const total=all ? prefix.at(-1):0;
  return {ratio:all ? (prefix[position]+t)/total : (position+segFraction)/Math.max(1,items.length),all,total,elapsed:all ? prefix[position]+t:t};
}
function updateBar() {
  const play=get('pt-play'),range=get('pt-timeline'); if(!play||!range)return;
  const action=playing||pending?labels('暂停','Pause'):labels('播放','Play');
  if(play.title!==action) {play.replaceChildren(icon(playing||pending?'pause':'play'));play.title=action;play.setAttribute('aria-label',action);}
  play.disabled=!items.length; range.disabled=!items.length;
  const device=deviceMode(), timing=sliderValue();
  if(document.activeElement!==range || range.dataset.drag!=='yes') range.value=String(10000* Math.min(1,timing.ratio));
  const count=(position+Number(items.length>0))+' / '+items.length;
  const clock=get('pt-clock');
  clock.textContent=timing.all ? time(timing.elapsed)+' / '+time(timing.total):time(timing.elapsed)+(clipDuration(current()||{})?' / '+time(clipDuration(current())):'');
  if(device)clock.textContent=labels('系统语音','Device voice');
  clock.title=timing.all?labels('整段播放时间','Full track time'):labels('当前句的播放时间','Current sentence time');
  const section=current()?.sectionTitle||doc?.title||'';
  get('pt-track-title').textContent=doc?.title || section;
  const sectionName=section && section!==doc?.title ? section+' · ' : '';
  get('pt-counter').textContent=(pending?(device?labels('载入语音 · ','Loading voice · '):labels('正在生成音频 · ','Preparing audio · ')):'')+sectionName+labels('句 ','Sentence ')+count;
  const desc=labels('定位到句子 ','Seek to sentence ')+count;
  range.setAttribute('aria-valuetext',desc);
  const live=get('pt-message'); if(live.textContent!==playbackAlert) live.textContent=playbackAlert;
}
function chooseMode(val) {
  if(val==='zh'||val==='en'){switchTrack(val);return;}
  if(deviceMode()){
    if(val==='tutorial' && doc.sample_tutorial){switchTrack('tutorial',doc.sample_tutorial);openTutor();return;}
    if(val==='explanation'){
      const last=storage.read(prefsKey(),{}).explanation;
      const entry=Object.entries(doc.sample_explanations || {}).find(([,r])=>r.id===last);
      if(entry){switchTrack(val,entry[1]);showTutor(entry[0]);return;}
    }
  }
  const saved=storage.read(recordKey(),{})[val==='tutorial'?'paper':'lastEquation'];
  if(saved?.result){switchTrack(val,saved.result);showTutor(saved.eqid || null,saved);return;}
  if(val==='tutorial') {openTutor();renderBar();}
  else {feedback(labels('请先在正文中选择一个公式。','Choose an equation in the paper first.'));renderBar();}
}
function renderBar() {
  const root=get('player-root');if(!root)return;
  root.replaceChildren();if(!doc)return;
  const bar=element('section','pt-audio-bar');bar.id='pt-audio-bar';bar.setAttribute('aria-label',labels('论文朗读','Paper narration'));
  const main=element('div','pt-bar-main'), tag=element('div','pt-player-mark');tag.append(icon('sound'));
  const meta=element('div','pt-track');const title=element('a','pt-track-link');title.id='pt-track-title';title.href='#/paper/'+encodeURIComponent(doc.id);
  title.setAttribute('aria-label',labels('打开论文','Open paper')+' · '+doc.title);
  title.title=doc.title;const counter=element('small');counter.id='pt-counter';meta.append(title,counter);
  const transport=element('div','pt-transport');
  const prev=button(labels('上一句','Previous sentence'),()=>seek(position-1),'pt-icon-btn','prev');
  const play=button('',toggle,'pt-icon-btn pt-play','play');play.id='pt-play';
  const next=button(labels('下一句','Next sentence'),()=>seek(position+1),'pt-icon-btn','next');
  transport.append(prev,play,next);
  const timeline=element('div','pt-progress');const range=element('input');range.type='range';range.min='0';range.max='10000';range.step='1';range.id='pt-timeline';
  range.setAttribute('aria-label',labels('朗读进度','Narration position'));range.addEventListener('pointerdown',()=>range.dataset.drag='yes');
  range.addEventListener('pointerup',()=>range.dataset.drag='no');range.addEventListener('pointercancel',()=>range.dataset.drag='no');
  range.addEventListener('change',()=>{
    const ratio=Number(range.value)/10000;
    if(deviceMode()){seek(Math.min(items.length-1,Math.floor(items.length*ratio)));return;}
    const t=seekTarget(items.map(clipDuration),ratio);seek(t.index,t.time,t.fraction);
  });
  const clock=element('span','pt-clock');clock.id='pt-clock';timeline.append(range,clock);
  const controls=element('div','pt-options');
  const modes=[['zh',labels('中文译文','Chinese')],['en',labels('英文原文','English')],['tutorial',labels('论文讲解','Paper tutorial')],['explanation',labels('公式讲解','Equation explanation')]];
  if(mode==='equation') modes.push(['equation',labels('公式朗读','Equation reading')]);
  const picker=selection(labels('播放内容','Narration mode'),modes,mode,chooseMode);picker.className='pt-mode';controls.append(picker);
  const menu=button(labels('播放设置和下载','Playback settings and downloads'),()=>{optionsOpen=!optionsOpen;renderBar();get('pt-settings-btn').focus();},'pt-icon-btn','settings');
  menu.id='pt-settings-btn';menu.setAttribute('aria-expanded',String(optionsOpen));controls.append(menu);
  main.append(tag,meta,transport,timeline,controls);
  const msg=element('div','pt-message');msg.id='pt-message';msg.setAttribute('role','status');bar.append(main,msg);
  if(optionsOpen) {
    const opts=element('div','pt-options-panel');const head=element('div','pt-opt-head');head.append(element('strong','',labels('播放设置','Playback settings')),button(labels('关闭','Close'),()=>{optionsOpen=false;renderBar();},'pt-icon-btn','close'));opts.append(head);
    const speed=selection(labels('语速','Speed'),[.5,.75,1,1.25,1.5,1.75,2,2.25,2.5].map(x=>[x,x+'×']),settings.speed,v=>{settings.speed=Number(v);if(audio)audio.playbackRate=settings.speed;saveOptions();if(utterance&&playing)startAt(position);});
    const alter = (key,v) => {
      const id=current()?.id, run=pending||playing;save();abortPlayback();settings[key]=v;saveOptions();items=buildTrack();position=Math.max(0,items.findIndex(s=>s.id===id));offset=0;deferredPosition=null;
      playbackAlert='';updateBar();if(run) startAt(position);
    };
    const systemVoices=window.speechSynthesis?.getVoices?.().filter(v=>v.lang.startsWith(mediaLang())) || [];
    const voice=deviceMode()?selection(labels('声音','Voice'),[['',labels('系统默认','Device default')],...systemVoices.map(v=>[v.voiceURI,v.name])],settings.deviceVoice || '',v=>alter('deviceVoice',v)):selection(labels('声音','Voice'),offeredVoices.map(x=>[x,x[0].toUpperCase()+x.slice(1)]),settings.voice,v=>alter('voice',v));
    const formulas=selection(labels('公式朗读','Read formulas'),[['skip',labels('跳过','Skip')],['brief',labels('简略朗读','Brief')],['full',labels('完整朗读','Full')]],settings.formula_mode,v=>alter('formula_mode',v));
    const follow=element('input');follow.type='checkbox';follow.checked=settings.follow;follow.addEventListener('change',()=>{settings.follow=follow.checked;saveOptions();});
    opts.append(labelled(labels('语速','Speed'),speed),labelled(deviceMode()?labels('系统声音','Device voice'):labels('合成声音','AI voice'),voice),labelled(labels('公式朗读','Read formulas'),formulas),labelled(labels('跟随朗读滚动','Follow narration'),follow));
    const nav=element('div','pt-menu-nav');
    for(const [name,fn] of [[labels('上一段','Previous paragraph'),()=>seek(groupJump(items,position,-1,'paragraphId'))],[labels('下一段','Next paragraph'),()=>seek(groupJump(items,position,1,'paragraphId'))],[labels('上一节','Previous section'),()=>seek(groupJump(items,position,-1,'sectionId'))],[labels('下一节','Next section'),()=>seek(groupJump(items,position,1,'sectionId'))],[labels('重读本句','Replay sentence'),()=>startAt(position)],[labels('停止','Stop'),()=>{abortPlayback();engaged=false;offset=0;deferredPosition=null;save();updateBar();}]]) nav.append(button(name,fn));
    opts.append(nav);
    const download=element('div','pt-dl');
    if(deviceMode()){download.append(element('small','pt-secondary',labels('MP3 导出可在完整应用中使用。','MP3 exports are available in the full application.')));}
    else if(activeJob) {
      const state=element('span','pt-secondary');state.id='pt-export-state';state.textContent=activeJob.message || labels('正在准备 MP3…','Preparing MP3…');download.append(state);
      if(activeJob.status==='ready') {
        const a=element('a','pt-primary',labels('下载 MP3','Download MP3'));a.href=rootPath()+'/audio-export/'+encodeURIComponent(activeJob.id)+'/file';a.download='';download.append(a);
        download.append(button(labels('其他导出','More exports'),()=>{clearJob();renderBar();}));
      } else if(activeJob.status==='failed') download.append(button(labels('返回','Back'),()=>{clearJob();renderBar();}));
      else download.append(button(labels('取消','Cancel'),cancelExport));
    } else {
      const name=mode==='tutorial'||mode==='explanation'?labels('生成讲解 MP3','Generate tutorial MP3'):labels('生成全文 MP3','Generate full MP3');
      if(mode!=='equation') download.append(button(name,()=>startExport(false),'pt-primary'));
      if(bookMode()) download.append(button(labels('导出本节','Export section'),()=>startExport(true)));
      if(mode==='equation') download.append(button(labels('下载公式朗读','Download equation audio'),downloadEquation));
    }
    opts.append(download);bar.append(opts);
  }
  root.append(bar);updateBar();
}
function jobKey(){return 'pt-audio-export-'+doc.id;}
function clearJob() {if(doc)storage.remove(jobKey());activeJob=null;}
async function startExport(sectionOnly) {
  if(!doc||activeJob)return;
  if(!current()) return feedback(labels('没有可导出的朗读内容。','There is no narration to export.'));
  const id=doc.id; const token={id:null,status:'starting',message:labels('正在提交…','Starting…')};activeJob=token;renderBar();
  try {
    const response=await api(rootPath()+'/audio-export',{language:mediaLang(),formula_mode:settings.formula_mode,voice:settings.voice,
      ...(sectionOnly?{section_id:current().sectionId}:{}),...(!bookMode()?{explanation_id:explanation.id}:{})});
    const j=await response.json(); if(doc?.id!==id || activeJob!==token) return;
    if(!j.id) throw Error(labels('未收到音频任务编号。','Missing audio job ID.'));
    activeJob={id:j.id,status:j.status};storage.write(jobKey(),activeJob);renderBar();pollExport(id,j.id);
  }catch(err){if(doc?.id===id&&activeJob===token){clearJob();renderBar();feedback(messageFrom(err));}}
}
async function pollExport(paperId,jobId) {
  if(doc?.id!==paperId||activeJob?.id!==jobId)return;
  try {
    const r=await api(rootPath()+'/audio-export/'+encodeURIComponent(jobId),null,undefined,'GET');const j=await r.json();
    if(doc?.id!==paperId || activeJob?.id!==jobId) return;
    if(['done','completed','complete','ready'].includes(j.status)){activeJob={id:jobId,status:'ready',message:labels('MP3 已就绪','MP3 is ready')};}
    else if(['error','failed','cancelled','canceled'].includes(j.status)){activeJob={id:jobId,status:'failed',message:j.error || labels('导出已停止。','Export stopped.')};}
    else{
      const done=j.progress?.done ?? j.done, total=j.progress?.total ?? j.total;
      activeJob={id:jobId,status:j.status,message:labels('生成 MP3','Generating MP3')+(Number.isFinite(done)&&Number.isFinite(total)?' · '+done+' / '+total:'…')};
    }
    const elem=get('pt-export-state');if(elem)elem.textContent=activeJob.message;
    storage.write(jobKey(),activeJob);
    if(['ready','failed'].includes(activeJob.status)){renderBar();return;}
  }catch(err){
    if(doc?.id!==paperId || activeJob?.id!==jobId)return;
    if(err.status===404 || err.status===410){
      activeJob={id:jobId,status:'failed',message:messageFrom(err)};storage.write(jobKey(),activeJob);renderBar();return;
    }
    activeJob.message=messageFrom(err);if(get('pt-export-state'))get('pt-export-state').textContent=activeJob.message;
  }
  setTimeout(()=>pollExport(paperId,jobId),2500);
}
async function cancelExport() {
  if(!activeJob?.id)return;
  try {await api(rootPath()+'/audio-export/'+encodeURIComponent(activeJob.id),null,undefined,'DELETE');clearJob();renderBar();}
  catch(err){feedback(messageFrom(err));}
}
async function downloadEquation() {
  const cur=current();if(!cur)return;
  try{const url=await loadClip(cur,new AbortController().signal),a=element('a');a.href=url;a.download='equation.mp3';a.click();}catch(err){feedback(messageFrom(err));}
}
function closeDrawer() {
  drawerNonce++;drawerAbort?.abort();drawerAbort=null;drawer=null;
  const root=get('tutor-root');if(root)root.replaceChildren();
}
function panelShell(title,sub) {
  const root=get('tutor-root');if(!root)return null;root.replaceChildren();
  const panel=element('aside','pt-drawer');panel.setAttribute('role','dialog');panel.setAttribute('aria-label',title);
  const head=element('div','pt-drawer-head');const names=element('div');
  names.append(element('small','pt-eyebrow',sub),element('h2','',title));head.append(names,button(labels('关闭面板','Close panel'),closeDrawer,'pt-icon-btn','close'));
  const main=element('div','pt-drawer-body');panel.append(head,main);root.append(panel);return main;
}
function math(target,tex,display=false) {
  if(typeof window.PaperTranslate?.math==='function'){try{window.PaperTranslate.math(target,tex,display);return;}catch{}}
  target.textContent=tex;
}
function formula(target,eq) {
  if(eq?.latex){math(target,eq.render_latex || eq.latex,true);return;}
  if(eq?.image&&/^assets\/[a-z\d_.-]+$/i.test(eq.image)){const img=element('img');img.src=rootPath()+'/assets/'+encodeURIComponent(eq.image.slice(7));img.alt=labels('原文公式','Original equation');target.append(img);}
  else target.textContent=eq?.raw || labels('公式图像不可用','Equation image unavailable');
}
function richText(target,source) {
  const str=String(source || '');
  const re=/\{\{eq:([\w.:-]+)\}\}|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)|\$\$([\s\S]*?)\$\$|\$([^$\n]+)\$/g;
  let last=0;
  for(const m of str.matchAll(re)){
    target.append(document.createTextNode(str.slice(last,m.index)));
    const n=element('span','pt-math-inline');
    if(m[1] && doc.equations?.[m[1]]) {
      const eq=doc.equations[m[1]]; if(eq.latex)math(n,eq.render_latex || eq.latex,!!eq.display);else formula(n,eq);
    }else if(m[1])n.textContent=m[0];else math(n,m[2] || m[3] || m[4] || m[5],!!(m[2]||m[4]));
    target.append(n);last=m.index+m[0].length;
  }
  target.append(document.createTextNode(str.slice(last)));
}
function equationSentence(eqid) {
  const token='{{eq:'+eqid+'}}';const track=paperTrack(doc,'en','full');
  return track.find(s=>s.en?.includes(token)||s.zh?.includes(token)) || track.find(s=>s.paragraphId===doc.equations[eqid]?.paragraph_id);
}
function addError(parent,msg) {
  const err=element('p','pt-drawer-error',msg);err.setAttribute('role','alert');parent.append(err);
}
async function equationVoice(id,body,lang,style) {
  const owner=equationSentence(id), ticket=++drawerNonce;if(!owner)return;
  drawerAbort?.abort();drawerAbort=new AbortController();
  const zone=element('section','pt-voice-edit');body.querySelector('.pt-voice-edit')?.remove();body.append(zone);
  zone.textContent=labels('正在准备读音…','Preparing pronunciation…');
  if(deviceMode()){showVoiceEditor(zone,id,owner,lang,deviceFormula(id,lang,style));return;}
  try{
    const resp=await api(rootPath()+'/speech-preview',{text:'{{eq:'+id+'}}',language:lang,formula_mode:style},drawerAbort.signal);
    const j=await resp.json();if(drawerNonce!==ticket)return;
    showVoiceEditor(zone,id,owner,lang,j.text || '');
  }catch(err){
    if(drawerNonce!==ticket || err.name==='AbortError')return;
    showVoiceEditor(zone,id,owner,lang,'');addError(zone,messageFrom(err));
  }
}
function showVoiceEditor(zone,id,owner,lang,pronunciation) {
  zone.replaceChildren(element('strong','',labels('朗读文本','Spoken text')));
  const field=element('textarea');field.rows=4;field.maxLength=4000;field.value=pronunciation;field.setAttribute('aria-label',labels('预览或修改公式读音','Preview or edit pronunciation'));
  const play=button(labels('播放公式','Play equation'),()=>{if(!field.value.trim())return;
    switchTrack('equation',null,{...owner,eqid:id,lang,override:field.value.trim()});startAt(0);},'pt-primary');
  zone.append(field,play);field.focus();
}
export function openEquation(id) {
  if(!doc?.equations?.[id])return;
  closeDrawer();drawer={kind:'equation',id};const eq=doc.equations[id];
  const body=panelShell(labels('公式工具','Equation tools'),eq.label || labels('论文原文','Original paper'));if(!body)return;
  const box=element('div','pt-formula');formula(box,eq);body.append(box);
  const actions=element('div','pt-equation-actions');body.append(actions);
  const savedKey=storage.read(recordKey(),{})['eq-'+id];
  actions.append(button(labels('解释公式','Explain equation'),()=>{showTutor(id,savedKey);if(!savedKey?.result)requestExplanation('detailed');},'pt-primary'),
    button(labels('逐步推导','Show derivation'),()=>{showTutor(id);requestExplanation('derivation');}),
    button(labels('向 AI 提问','Ask AI'),()=>{showTutor(id,savedKey);get('pt-question')?.focus();}));
  if(eq.latex)actions.append(button(labels('复制 LaTeX','Copy LaTeX'),async()=>{try{await navigator.clipboard.writeText(eq.latex);feedback(labels('已复制公式','Equation copied'));}catch{const f=element('textarea');f.value=eq.latex;body.append(f);f.select();}}));
  const voice=element('div','pt-equation-voice');
  const lang=selection(labels('朗读语言','Language'),[['zh','中文'],['en','English']],mediaLang(),()=>{});
  const style=selection(labels('读法','Reading style'),[['brief',labels('简略','Brief')],['full',labels('完整','Full')]],settings.formula_mode==='skip'?'full':settings.formula_mode,()=>{});
  voice.append(lang,style,button(labels('朗读公式','Read equation'),()=>equationVoice(id,body,lang.value,style.value)));
  body.append(voice);
}
export function openTutor(){if(doc)showTutor(null,storage.read(recordKey(),{}).paper);}
function showTutor(eqid,saved=null) {
  closeDrawer();
  const authored=deviceMode() ? (eqid?doc.sample_explanations?.[eqid]:doc.sample_tutorial):null;
  drawer={kind:'tutor',eqid,result:authored || saved?.result || null,authored:Boolean(authored),history:saved?.history || [],depth:saved?.depth || (eqid?'detailed':'concise'),busy:false};
  renderTutor();
}
function refLink(ref) {
  let context;
  for(const sec of doc.sections||[]) {
    const p=sec.paragraphs?.find(x=>x.id===ref);if(p){context={sec,p};break;}
  }
  if(!context)return null;
  const excerpt=(context.p.sentences?.[0]?.[locale] || context.p.sentences?.[0]?.en || '').replace(eqPattern,labels('公式','equation'));
  const title=context.sec.title+' · '+ref;
  const b=button(title,()=>{closeDrawer();window.PaperTranslate?.goParagraph?.(ref);},'pt-reference');b.title=excerpt ||title;return b;
}
function renderTutor() {
  const view=drawer;if(view?.kind!=='tutor')return;
  const eqid=view.eqid;
  const title=eqid?labels('公式讲解','Equation explanation'):view.authored?labels('示例论文讲解','Sample paper tutorial'):labels('AI 论文讲解','AI paper tutorial');
  const body=panelShell(title,eqid?(doc.equations[eqid]?.label||labels('原文公式','Original equation')):'PaperTranslate');if(!body)return;
  if(eqid){
    const box=element('div','pt-formula');formula(box,doc.equations[eqid]);body.append(box);
    body.append(button(labels('返回公式工具','Equation tools'),()=>openEquation(eqid),'pt-return'));
  }
  const choices=eqid?[['quick',labels('快速解释','Quick')],['detailed',labels('详细解释','Detailed')],['derivation',labels('数学推导','Derivation')]]:
    [['concise',labels('简要概述','Concise')],['detailed',labels('详细讲解','Detailed')],['research',labels('研究级讨论','Research level')]];
  const line=element('div','pt-depth');
  const select=selection(labels('讲解深度','Depth'),choices,view.depth,v=>view.depth=v);
  const gen=button(view.busy?labels('生成中…','Generating…'):labels('生成讲解','Generate explanation'),()=>requestExplanation(select.value),'pt-primary');gen.disabled=view.busy;line.append(select,gen);if(!view.authored)body.append(line);
  if(view.error){
    addError(body,view.error);
    if(deviceMode() && window.PaperTranslate?.offlineHelp)body.append(button(labels('打开完整应用','Open full application'),window.PaperTranslate.offlineHelp));
  }
  const r=view.result;
  if(r){
    const toolbar=element('div','pt-result-tools');
    toolbar.append(button(labels('朗读讲解','Listen to explanation'),()=>{
      const next=eqid?'explanation':'tutorial';
      if(mode!==next||explanation?.id!==r.id)switchTrack(next,r);
      if(!playing&&!pending)toggle();
    }));
    toolbar.append(element('small','pt-secondary',view.authored?labels('PaperTranslate 编写的示例','Authored by PaperTranslate'):labels('AI 生成','AI generated')));body.append(toolbar);
    const known=new Set();
    const categories={source:labels('论文陈述','In the paper'),deduced:labels('数学推论','Deduction'),interpretation:view.authored?labels('编者解读','Editorial interpretation'):labels('AI 解读','AI interpretation'),uncertain:labels('尚待验证','Unverified')};
    let heading='',kind='';
    for(const s of r.sentences||[]){
      if(s.heading&&s.heading!==heading){heading=s.heading;body.append(element('h3','pt-step-heading',heading));kind='';}
      if(s.kind&&s.kind!==kind && categories[s.kind]){body.append(element('small','pt-source-kind',categories[s.kind]));kind=s.kind;}
      const row=element('div','pt-tutor-row');const line=element('button','pt-tutor-sentence');line.type='button';line.dataset.tutorSentence=s.id;
      richText(line,s.zh || s.en);line.addEventListener('click',()=>{
        if(mode!==(eqid?'explanation':'tutorial') || explanation?.id!==r.id)switchTrack(eqid?'explanation':'tutorial',r);
        const index=items.findIndex(x=>x.id===s.id);if(index>=0)startAt(index);
      });row.append(line);
      const refs=element('div','pt-refs');
      for(const ref of s.references||[]){const link=refLink(ref);if(link){refs.append(link);known.add(ref);}}
      if(refs.childElementCount)row.append(refs);body.append(row);
    }
    if(!r.sentences?.length){const para=element('div','pt-tutor-row');richText(para,r.text);body.append(para);}
    const refs=element('div','pt-refs');
    for(const ref of r.references||[]){if(known.has(ref))continue;const link=refLink(ref);if(link)refs.append(link);}
    if(refs.childElementCount)body.append(refs);
    if(playing&&explanation?.id===r.id)highlight(false);
  }
  const form=element('form','pt-ask');const q=element('textarea');q.rows=2;q.id='pt-question';q.maxLength=3000;
  q.setAttribute('aria-label',labels('追问','Follow-up question'));q.placeholder=eqid?labels('例如，这一步用了什么近似？','Which approximation is used in this step?'):labels('关于这篇论文，你还想了解什么？','Ask a question about the paper');
  const ask=button(labels('提问','Ask'),()=>{},'pt-tool');ask.type='submit';ask.disabled=view.busy;
  form.addEventListener('submit',e=>{e.preventDefault();if(q.value.trim())requestExplanation(select.value,q.value.trim());});form.append(q,ask);body.append(form);
}
async function requestExplanation(depth,question='') {
  const view=drawer;if(!doc || view?.kind!=='tutor' || view.busy)return;
  if(deviceMode()){view.error=view.authored && !question ? '' : serviceMessage();renderTutor();return;}
  const ticket=++drawerNonce;drawerAbort?.abort();drawerAbort=new AbortController();
  view.depth=depth;view.busy=true;view.error='';renderTutor();
  try {
    const response=await api(rootPath()+'/explain',{equation_id:view.eqid,depth,question,history:view.history},drawerAbort.signal);
    const r=await response.json();if(ticket!==drawerNonce)return;
    if(!r.id || !Array.isArray(r.sentences) || !r.sentences.length)throw Error(labels('讲解缺少可朗读的段落。','The explanation does not contain any narration segments.'));
    const history=[...view.history,{role:'user',content:question || depth},{role:'assistant',content:String(r.text || r.sentences.map(s=>s.zh).join('\n')).slice(0,9000)}].slice(-8);
    view.result=r;view.history=history;
    const data=storage.read(recordKey(),{}),saved={eqid:view.eqid,depth,history,result:r};
    data[view.eqid?'eq-'+view.eqid:'paper']=saved;if(view.eqid)data.lastEquation=saved;storage.write(recordKey(),data);
  }catch(err){if(ticket!==drawerNonce || err.name==='AbortError')return;view.error=messageFrom(err);}
  if(ticket===drawerNonce){view.busy=false;renderTutor();}
}
export function setDocument(value) {
  if(!value?.id) {closeDocument();return;}
  if(doc?.id===value.id) {
    const id=current()?.id;doc=value;items=buildTrack();
    if(id){const i=items.findIndex(s=>s.id===id);if(i>=0)position=i;}
    if(playing)highlight(false);updateBar();return;
  }
  closeDocument();doc=value;mode='zh';durationMap=storage.read(durKey(),{});
  const saved=storage.read(prefsKey(),{}),records=storage.read(recordKey(),{});
  if(saved.mode==='en')mode='en';
  else if(['tutorial','explanation'].includes(saved.mode)){
    const authored=deviceMode() && (saved.mode==='tutorial'?doc.sample_tutorial:Object.values(doc.sample_explanations || {}).find(r=>r.id===saved.explanation));
    const r=authored || records[saved.mode==='tutorial'?'paper':'lastEquation']?.result;
    if(r){mode=saved.mode;explanation=r;}
  }
  items=buildTrack();restore();document.body.classList.add('pt-has-player');renderBar();
  activeJob=storage.read(jobKey(),null);if(activeJob?.id&&!['failed','ready'].includes(activeJob.status))pollExport(doc.id,activeJob.id);
}
export function closeDocument() {
  if(doc)save();abortPlayback();closeDrawer();disposeClips();doc=null;items=[];explanation=literal=null;
  durationMap={};engaged=false;optionsOpen=false;activeJob=null;position=offset=0;deferredPosition=null;
  get('player-root')?.replaceChildren();document.body.classList.remove('pt-has-player');
}
export function setLanguage(lang) {
  const next=lang==='en'?'en':'zh';if(locale===next)return;locale=next;if(!doc)return;
  renderBar();if(drawer?.kind==='tutor')renderTutor();else if(drawer?.kind==='equation')openEquation(drawer.id);
}
if(typeof window!=='undefined'){
  window.addEventListener('pagehide',save);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')save();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(drawer)closeDrawer();else if(optionsOpen){optionsOpen=false;renderBar();get('pt-settings-btn')?.focus();}}});
}
