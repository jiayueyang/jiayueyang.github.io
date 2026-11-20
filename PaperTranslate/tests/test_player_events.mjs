// Deterministic event ordering complements the real-media browser acceptance suite.
import test from 'node:test';
import assert from 'node:assert/strict';

function harness() {
  class El {
    constructor(tag) {this.tag=tag;this.children=[];this.events={};this.dataset={};this.attrs={};this.title='';this.textContent='';this.classes=new Set();
      this.classList={add:n=>this.classes.add(n),remove:n=>this.classes.delete(n)};}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this.children=nodes;}
    setAttribute(k,v){this.attrs[k]=String(v);}
    getAttribute(k){return this.attrs[k];}
    addEventListener(k,f){this.events[k]=f;}
    get childElementCount(){return this.children.length;}
    getClientRects(){return [1];}
    getBoundingClientRect(){return {top:300,bottom:320};}
    closest(selector){return selector==='[data-lang]'?this:null;}
    scrollIntoView(){}
  }
  const body=new El('body');
  function create(tag,id='') {const el=new El(tag);el.id=id;return el;}
  const player=create('div','player-root'),drawer=create('div','tutor-root');body.append(player,drawer);
  function visit(el,id){if(el.id===id)return el;for(const n of el.children){const found=visit(n,id);if(found)return found;}}
  const paragraphs={},sentences={};
  for(const [sid,pid] of [['s1','p1'],['s2','p2'],['s3','p3']]){
    sentences[sid]=['zh','en'].map(lang=>{const e=new El('span');e.dataset.lang=lang;return e;});
    paragraphs[pid]=[new El('p'),new El('p')];
  }
  globalThis.document={body,createTextNode:text=>({textContent:text,children:[]}),createElement:create,createElementNS:(_ns,tag)=>create(tag),getElementById:id=>visit(body,id),
    addEventListener(){},querySelectorAll(selector){let match=selector.match(/^\[data-(sentence|paragraph)="([^"]+)"/);return (match[1]==='sentence'?sentences:paragraphs)[match[2]] || [];}};
  globalThis.window={addEventListener(){},innerHeight:900};
  globalThis.CSS={escape:String};
  const storage=new Map();
  globalThis.localStorage={setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k)??null,removeItem:k=>storage.delete(k)};
  const sessions=[],requests=[];
  globalThis.fetch=async(url,params)=>{
    const b=JSON.parse(params.body);requests.push({url,...b});
    return {ok:true,blob:async()=>new Blob(['test fixture'],{type:'audio/mpeg'})};
  };
  globalThis.Audio=class {
    constructor(){this.currentTime=0;this.duration=4;this.paused=true;sessions.push(this);}
    async play(){if(!this.ready){this.ready=true;this.onloadedmetadata?.();}this.paused=false;this.onplaying?.();}
    pause(){this.paused=true;}
    load(){}
    removeAttribute(){}
    end(){this.currentTime=this.duration;this.ended=true;this.onended?.();}
  };
  function all(el) {return [el,...el.children.flatMap(all)];}
  return {sessions,requests,sentences,paragraphs,get:id=>visit(body,id),findText:text=>all(body).find(n=>n.textContent===text),storage};
}
const tick=()=>new Promise(r=>setImmediate(r));
test('segments advance on ended, stay aligned when seeking, pause/resume, and restore per-language progress',async()=>{
  const h=harness();const p=await import('../web/player.js');
  const doc={id:'demo',title:'Demo',sections:[{id:'s',title:'Setup',paragraphs:['1','2','3'].map(n=>({id:'p'+n,sentences:[{id:'s'+n,en:'Source '+n,zh:'译文'+n}]}))}]};
  p.setDocument(doc);assert.equal(p.getActivePaperId(),'demo');assert.equal(h.get('pt-track-title').href,'#/paper/demo');
  assert.equal(p.isEngaged(),false);p.readFrom('s1','zh');await tick();assert.equal(p.isEngaged(),true);
  const first=h.sessions.at(-1);
  assert.equal(h.sentences.s1[0].classes.has('pt-speaking'),true);
  assert.equal(h.sentences.s1[1].classes.has('pt-speaking'),false);
  assert.equal(h.paragraphs.p1.every(n=>n.classes.has('pt-speaking-paragraph')),true);
  assert.equal(h.requests.some(r=>r.sentence_id==='s2'),true);
  first.currentTime=1.25;first.ontimeupdate();h.get('pt-play').events.click();
  assert.equal(first.paused,true);h.get('pt-play').events.click();
  assert.equal(first.paused,false);assert.equal(first.currentTime,1.25);
  first.end();await tick();
  assert.equal(h.sentences.s2[0].classes.has('pt-speaking'),true);
  const slider=h.get('pt-timeline');slider.value='9000';slider.events.change();await tick();
  assert.equal(h.sentences.s3[0].classes.has('pt-speaking'),true);
  assert.ok(Math.abs(h.sessions.at(-1).currentTime-2.8)<1e-8);
  p.readFrom('s2','en');await tick();
  assert.equal(h.sentences.s2[1].classes.has('pt-speaking'),true);
  h.sessions.at(-1).currentTime=.42;p.closeDocument();p.setDocument(doc);h.get('pt-play').events.click();await tick();
  assert.equal(h.sentences.s2[1].classes.has('pt-speaking'),true);
  assert.equal(h.sessions.at(-1).currentTime,.42);
  const prefs=JSON.parse(h.storage.get('pt-player-demo'));
  assert.equal(prefs.zh.id,'s3');assert.equal(prefs.en.id,'s2');
  p.closeDocument();
});
test('static sample uses utterance events and authored mathematical pronunciation',async()=>{
  const h=harness(),p=await import('../web/player.js');const voices=[];
  window.PaperTranslate={offline:()=>true};
  window.speechSynthesis={getVoices:()=>[],speak:u=>{voices.push(u);u.onstart();},cancel(){},pause(){},resume(){}};
  globalThis.SpeechSynthesisUtterance=class {constructor(text){this.text=text;}};
  const doc={id:'sample',title:'Demo',equations:{e1:{pronunciation:{zh:'能量等于质量乘以光速的平方。'}}},sections:[{id:'sec',title:'Energy',paragraphs:[{id:'p1',sentences:[
    {id:'s1',en:'Equation',zh:'{{eq:e1}}'},{id:'s2',en:'Second',zh:'结束。'}]}]}]};
  p.setDocument(doc);p.readFrom('s1','zh');
  assert.equal(voices[0].text,'能量等于质量乘以光速的平方。');
  assert.equal(h.sentences.s1[0].classes.has('pt-speaking'),true);
  voices[0].onend();assert.equal(voices[1].text,'结束。');
  assert.equal(h.sentences.s2[0].classes.has('pt-speaking'),true);
  assert.equal(h.requests.length,0);p.closeDocument();
});
test('Pages exposes authored equation and tutorial with local audio, source navigation and live-service boundary',async()=>{
  const h=harness(),p=await import('../web/player.js'),spoken=[];
  window.PaperTranslate={offline:()=>true,goParagraph:id=>{window.visited=id;}};
  window.speechSynthesis={getVoices:()=>[],speak:u=>{spoken.push(u);u.onstart();},cancel(){},pause(){},resume(){}};
  globalThis.SpeechSynthesisUtterance=class {constructor(text){this.text=text;}};
  const exp={id:'tutorial-sample',sentences:[{id:'s1',zh:'原文给出温度。',heading:'物理意义',kind:'interpretation',references:['p1']}]};
  const doc={id:'demoexplain',title:'Example',equations:{e1:{latex:'T=1',paragraph_id:'p1'}},sample_tutorial:exp,sample_explanations:{e1:{...exp,id:'eq-sample'}},sections:[
    {id:'sec',title:'Setup',paragraphs:[{id:'p1',sentences:[{id:'s1',en:'First.',zh:'第一句。'}]}]}]};
  p.setDocument(doc);p.openTutor();assert.ok(h.findText('PaperTranslate 编写的示例'));
  assert.ok(h.findText('编者解读'));assert.equal(h.findText('AI 解读'),undefined);
  h.findText('朗读讲解').events.click();assert.equal(spoken.at(-1).text,'原文给出温度。');
  h.findText('Setup · p1').events.click();assert.equal(window.visited,'p1');
  p.openEquation('e1');h.findText('逐步推导').events.click();assert.ok(h.findText('物理意义'));
  h.findText('朗读讲解').events.click();assert.equal(spoken.at(-1).text,'原文给出温度。');
  assert.equal(h.requests.length,0);p.closeDocument();
});
test('a late response from an old seek cannot replace the current audio or highlight',async()=>{
  const h=harness(),p=await import('../web/player.js');let release;
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{
    const b=JSON.parse(init.body);
    if(b.sentence_id==='s1')await new Promise(resolve=>{release=resolve;});
    return originalFetch(url,init);
  };
  const doc={id:'race',title:'Race',sections:[{id:'sec',title:'Setup',paragraphs:[
    {id:'p1',sentences:[{id:'s1',en:'First.',zh:'第一句。'}]},
    {id:'p2',sentences:[{id:'s2',en:'Second.',zh:'第二句。'}]},
  ]}]};
  p.setDocument(doc);p.readFrom('s1','zh');p.readFrom('s2','zh');await tick();release();await tick();
  assert.equal(h.sessions.length,1);assert.equal(h.sentences.s2[0].classes.has('pt-speaking'),true);
  assert.equal(h.sentences.s1[0].classes.has('pt-speaking'),false);p.closeDocument();
});
