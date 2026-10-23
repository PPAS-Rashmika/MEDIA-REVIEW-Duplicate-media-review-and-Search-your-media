const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const base = require('path').join(__dirname, '..', 'static') + require('path').sep;
const code = fs.readFileSync(base + 'file_tools.js','utf8');
const html = fs.readFileSync(base + 'index.html','utf8');
const elements = new Map();
const classes = new Map();
const renderedElements = new Set();
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(id='') {this.id=id;this._value='';this._html='';this.disabled=false;this.hidden=false;this.open=false;this.handlers={};this.dataset={};this.textContent='';this.attrs={};this.classList={add(){},toggle(name,v){classes.set(this.id+':'+name,v);}};}
  set value(value){this._value=String(value);}
  get value(){return this._value;}
  set innerHTML(value){
    this._html=value;
    for(const match of String(value).matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)){
      const attrs=Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(m=>[m[1],m[2]]));
      const el=attrs.id && elements.has(attrs.id) ? elements.get(attrs.id) : new Element(attrs.id);
      el.tagName=match[1].toUpperCase();el.parentElement=this;el.attrs=attrs;el.dataset={};
      for(const [name,value] of Object.entries(attrs))if(name.startsWith('data-'))el.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
      if(attrs.value!==undefined)el.value=attrs.value;
      el.hidden=/\bhidden\b/.test(match[2]);renderedElements.add(el);
      if(el.id)elements.set(el.id,el);
    }
  }
  get innerHTML(){return this._html;}
  addEventListener(name,fn){(this.handlers[name]||=[]).push(fn);}
  async emit(name,data={}){for(const fn of this.handlers[name]||[])await fn({preventDefault(){},target:this,...data});await flush();}
  setAttribute(name,value){this.attrs[name]=value;}
  append(child){if(child.id)elements.set(child.id,child);}
  showModal(){this.open=true;}
  close(){this.open=false;}
  click(){this.clicked=true;}
}
for(const match of html.matchAll(/\bid="([^"]+)"/g))elements.set(match[1],new Element(match[1]));
const $=id=>{if(!elements.has(id))throw Error('Missing element '+id);return elements.get(id);};
const created=[],calls=[],errors=[],toasts=[],downloads=[],applied=[];
let externalBusy=false,state={hasScanned:true,revision:1,photos:[]},confirmAnswer=true,rejectPlan=false,failExecute=false,failUndo=false;
const clickTarget=(data)=>({disabled:false,dataset:data,closest(){return this;}});
const toolButtons=()=>[...renderedElements].filter(el=>el.tagName==='BUTTON' && el.dataset.tool);
const querySelectorAll=selector=>{
  const matches=new Set();
  for(const part of selector.split(',').map(s=>s.trim())){
    if(part==='#tools-view button[data-tool]'){for(const el of toolButtons())matches.add(el);}
    else if(part==='#tools-view #tools-apply-keepers'){matches.add($('tools-apply-keepers'));}
    else if(part==='#tools-view #tools-report'){matches.add($('tools-report'));}
    else if(part==='[data-tool-undo]'){for(const el of renderedElements)if(el.dataset.toolUndo)matches.add(el);}
    else throw Error('Unexpected mock selector '+part);
  }
  return [...matches];
};
const planFiles=Array.from({length:55},(_,i)=>({name:i===0?'<img src=x onerror=alert(1)>':`file-${i}.txt`,source:`C:\\Generated\\file-${i}.txt`,destination:`C:\\Generated\\new-${i}.txt`,size:8,keeper:{source:'C:\\Generated\\original.txt'}}));
const context=vm.createContext({console,Date,Number,String,Object,Array,Math,Error,Boolean,
  window:{confirm(){return confirmAnswer;}},
  document:{getElementById:$,createElement(){const el=new Element();created.push(el);return el;},body:new Element('body'),querySelector(){return new Element('main');},querySelectorAll},
  URL:{createObjectURL(){return 'blob:generated-test';},revokeObjectURL(){}},setTimeout(){},
  async fetch(path){downloads.push(path);return{ok:true,async blob(){return{};}};}
});
vm.runInContext(code,context);
const ui=context.window.MediaReviewFileTools;
ui.init({getState:()=>state,isBusy:()=>externalBusy,error:m=>errors.push(m),toast:m=>toasts.push(m),applyKeepers:value=>applied.push(value),
  async loadState(value){state=value;},
  async request(path,body){calls.push({path,body});
    if(path==='/api/tools/plan'){if(rejectPlan)throw Error('Stale generated files');return{planId:'generated-plan',kind:body.kind,files:planFiles,count:planFiles.length,totalSize:440,permanent:body.kind==='purge-trash',note:body.kind==='hardlinks'?'Shared content note':''};}
    if(path==='/api/tools/execute'){
      if(failExecute)throw Object.assign(Error('Generated partial execute'),{result:{completed:1,failures:[{name:'file-conflict.txt',error:'Generated conflict'}],state:{...state,revision:state.revision+1}}});
      return{completed:55,failures:[],state:{...state,revision:state.revision+1}};
    }
    if(path==='/api/tools/history')return{history:[{id:'generated-operation',kind:'rename',createdAt:'2026-10-21T00:00:00Z',completed:2,undoAvailable:true}]};
    if(path==='/api/tools/undo'){
      if(failUndo)throw Object.assign(Error('Generated partial undo'),{result:{completed:1,failures:[{name:'undo-conflict.txt',error:'Generated undo conflict'}],state:{...state,revision:state.revision+1}}});
      return{completed:2,failures:[],state:{...state,revision:state.revision+1}};
    }
    if(path==='/api/tools/keepers')return{keepers:{group1:'photo1'}};
    throw Error('Unexpected request '+path);
  }
});
(async()=>{
  assert.equal(calls.length,0,'Component initialization must not start scans or file operations');
  const preview=async kind=>{await $('tools-view').emit('click',{target:clickTarget({tool:kind})});await flush();};
  await preview('hardlinks');
  assert.equal(calls[0].path,'/api/tools/plan');
  assert.equal(calls.filter(c=>c.path.endsWith('/execute')).length,0,'Preview must not execute');
  assert.equal($('tools-plan-dialog').open,true);
  assert($('tools-plan-list').innerHTML.includes('&lt;img'));
  assert(!$('tools-plan-list').innerHTML.includes('<img src=x'));
  assert.equal($('tools-plan-note').textContent,'Shared content note');
  assert.equal(($('tools-plan-list').innerHTML.match(/<article>/g)||[]).length,50);
  await $('tools-plan-next').emit('click');
  assert.equal(($('tools-plan-list').innerHTML.match(/<article>/g)||[]).length,5);
  assert.equal($('tools-plan-next').disabled,true);
  await $('tools-cancel').emit('click');
  assert.equal($('tools-plan-dialog').open,false);
  assert.equal(calls.filter(c=>c.path.endsWith('/execute')).length,0);
  await preview('purge-trash');
  assert.equal($('tools-confirm').disabled,true);
  $('tools-delete-word').value='delete';await $('tools-delete-word').emit('input');
  assert.equal($('tools-confirm').disabled,true);
  $('tools-delete-word').value='DELETE';await $('tools-delete-word').emit('input');
  assert.equal($('tools-confirm').disabled,false);
  await $('tools-confirm').emit('click');
  assert.equal(calls.filter(c=>c.path.endsWith('/execute')).length,1);
  assert.equal(calls.find(c=>c.path.endsWith('/execute')).body.confirmed,true);
  assert.equal(state.revision,2);
  assert.equal($('tools-plan-dialog').open,false);
  assert($('tools-history').innerHTML.includes('data-tool-undo="generated-operation"'));
  externalBusy=true;ui.setBusy(true);const count=calls.length;
  await preview('rename');assert.equal(calls.length,count,'Busy scan blocks operation planning');
  assert.equal($('tools-apply-keepers').disabled,true);
  assert(toolButtons().length>=6,'Mock discovers anonymous mutation buttons');
  assert(toolButtons().every(button=>button.disabled),'Busy scan disables every mutation button');
  externalBusy=false;ui.setBusy(false);
  state={...state,catalogOnly:true};ui.setBusy(false);
  assert(toolButtons().every(button=>button.disabled),'Offline catalogs disable every mutation button');
  assert.equal($('tools-report').disabled,false,'Offline catalog reports remain exportable');
  assert(querySelectorAll('[data-tool-undo]').every(button=>button.disabled),'Offline catalogs disable operation undo');
  state={...state,catalogOnly:false};ui.setBusy(false);
  assert(toolButtons().every(button=>!button.disabled),'Returning to a local library unlocks mutation buttons');
  $('tools-keep-rule').value='format';$('tools-formats').value='png, jpg';
  await $('tools-apply-keepers').emit('click');
  assert.equal(calls.at(-1).body.rule,'format');
  assert.equal(calls.at(-1).body.formats,'png, jpg');
  assert.equal(applied[0].group1,'photo1');
  await $('tools-report').emit('click');
  assert.equal(downloads[0],'/api/report.csv');
  assert(created.some(e=>e.download==='Media Review duplicates.csv'&&e.clicked));
  confirmAnswer=false;const undoCount=calls.length;
  await $('tools-history').emit('click',{target:clickTarget({toolUndo:'generated-operation'})});
  assert.equal(calls.length,undoCount,'Canceling undo must not change files');
  confirmAnswer=true;
  await $('tools-history').emit('click',{target:clickTarget({toolUndo:'generated-operation'})});
  assert.equal(calls.find(c=>c.path.endsWith('/undo')).body.confirmed,true);
  assert.equal(state.revision,3);
  rejectPlan=true;await preview('rename');
  assert(errors.includes('Stale generated files'));
  assert.equal(calls.filter(c=>c.path==='/api/scan').length,0);
  assert.equal(calls.filter(c=>c.path.endsWith('/execute')).length,1);
  rejectPlan=false;failExecute=true;await preview('rename');await $('tools-confirm').emit('click');
  assert.equal(state.revision,4,'Partial execute error refreshes the returned library state');
  assert.equal($('tools-plan-dialog').open,false,'A partially applied plan cannot be executed again');
  assert($('tools-status').textContent.includes('1 completed.'));
  assert($('tools-status').textContent.includes('Generated conflict'));
  assert(errors.includes('Generated partial execute'));
  failExecute=false;failUndo=true;
  await $('tools-history').emit('click',{target:clickTarget({toolUndo:'generated-operation'})});
  assert.equal(state.revision,5,'Partial undo error refreshes the returned library state');
  assert($('tools-status').textContent.includes('1 changes undone.'));
  assert(errors.includes('Generated partial undo'));
  assert.equal(ui.isBusy(),false,'Failed operations release the UI lock');
  console.log(JSON.stringify({ok:true,checks:['no automatic work','preview only','XSS-safe filenames','50-row pagination','cancel does not mutate','hardlink shared-content notice','permanent purge requires exact DELETE','explicit confirmed execution','state refresh after mutation','history rendering','busy scan blocks actions','all mutation buttons locked while busy','offline catalog mutations disabled','offline catalog CSV enabled','offline catalog undo disabled','local-library controls restored','custom keep-rule payload','keeper choices applied','CSV download','undo cancellation','explicit undo','stale-plan errors','no unexpected scans','partial execute state refresh','partial execute plan closed','partial execute conflict details','partial undo state refresh','partial undo error visible','operation lock released after errors']},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
