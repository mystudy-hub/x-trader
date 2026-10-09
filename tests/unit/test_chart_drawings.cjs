const test = require('node:test');
const assert = require('node:assert/strict');
const { DrawingTools, clipLine, distanceToSegment, channelShift, mmTarget, mmBoxLevels, validDrawing, readDrawingRecord } = require('../../web/drawings.js');

test('ray starts at its anchor and extends only in the chosen direction', () => {
  assert.deepEqual(clipLine({x:100,y:100},{x:200,y:50},300,200,'ray'),[{x:100,y:100},{x:300,y:0}]);
  assert.deepEqual(clipLine({x:100,y:100},{x:50,y:100},300,200,'ray'),[{x:100,y:100},{x:0,y:100}]);
  assert.deepEqual(clipLine({x:50,y:150},{x:50,y:50},300,200,'ray'),[{x:50,y:150},{x:50,y:0}]);
});
test('offscreen anchors can still have a visible ray, but an outward ray stays invisible', () => {
  assert.deepEqual(clipLine({x:-10,y:50},{x:-5,y:50},100,100,'ray'),[{x:0,y:50},{x:100,y:50}]);
  assert.equal(clipLine({x:-10,y:50},{x:-15,y:50},100,100,'ray'),null);
  assert.equal(clipLine({x:5,y:5},{x:5,y:5},100,100,'ray'),null);
});
test('channel uses price and bar coordinates, including a reversed baseline', () => {
  const a={logical:0,price:100}, b={logical:10,price:120}, c={logical:5,price:118};
  assert.equal(channelShift(a,b,c),8); assert.equal(channelShift(b,a,c),8);
  assert.equal(channelShift(a,{logical:0,price:120},c),null);
  assert.equal(distanceToSegment({x:50,y:4},{x:0,y:0},{x:100,y:0}),4);
});
test('persistent anchors reject malformed and nonfinite data', () => {
  const item={id:'saved',type:'ray',color:'#83acff',points:[{time:'2026-09-01',offset:0,price:100},{time:'2026-09-02',offset:.4,price:110}]};
  assert(validDrawing(item)); assert(!validDrawing({...item,type:'channel'}));
  assert(!validDrawing({...item,points:[item.points[0],{...item.points[1],price:Infinity}]}));
  assert(!validDrawing({...item,color:'url(unsafe)'}));
});

test('legacy equal-move drawings retain their original target geometry', () => {
  assert.deepEqual(mmTarget({logical:10,price:100},{logical:20,price:110}),{logical:30,price:120});
  assert.deepEqual(mmTarget({logical:10,price:100},{logical:20,price:90}),{logical:30,price:80});
  assert.deepEqual(mmTarget({logical:20,price:100},{logical:10,price:110}),{logical:0,price:120});
  assert.deepEqual(mmTarget({logical:10,price:100},{logical:10,price:110}),{logical:10,price:120});
});

test('legacy equal-move translation and saved anchors remain compatible', () => {
  const a={logical:10,price:100},b={logical:20,price:110};
  const before=mmTarget(a,b),after=mmTarget({logical:17,price:115},{logical:27,price:125});
  assert.deepEqual(after,{logical:before.logical+7,price:before.price+15});
  const points=[{time:'2026-09-01',offset:0,price:100},{time:'2026-09-02',offset:.4,price:110}];
  assert(validDrawing({id:'mm',type:'mm',color:'#83acff',points}));
  assert(!validDrawing({id:'mm',type:'mm',color:'#83acff',points:[...points,points[0]]}));
});

test('box MM projects 1H and 2H outside the correct breakout boundary', () => {
  const expected={high:110,low:100,height:10,up1:120,up2:130,down1:90,down2:80};
  assert.deepEqual(mmBoxLevels({price:100},{price:110}),expected);
  assert.deepEqual(mmBoxLevels({price:110},{price:100}),expected);
});

test('box MM ignores horizontal spacing and moves all targets with the box', () => {
  const before=mmBoxLevels({logical:10,price:100},{logical:20,price:110});
  assert.deepEqual(mmBoxLevels({logical:500,price:110},{logical:1,price:100}),before);
  const moved=mmBoxLevels({price:125},{price:135});
  assert.equal(moved.height,before.height);
  for(const field of ['low','high','up1','up2','down1','down2']) assert.equal(moved[field],before[field]+25);
});

test('box MM accepts two corners and rejects a zero-height saved box', () => {
  const points=[{time:'2026-09-01',offset:0,price:100},{time:'2026-09-02',offset:0,price:110}];
  assert(validDrawing({id:'box',type:'mm_box',color:'#83acff',points}));
  assert(!validDrawing({id:'box',type:'mm_box',color:'#83acff',points:[points[0],{...points[1],price:100}]}));
  assert(!validDrawing({id:'box',type:'mm_box',color:'#83acff',points:[...points,points[0]]}));
  const fractional=mmBoxLevels({price:1.25},{price:1.5});
  assert.deepEqual(fractional,{low:1.25,high:1.5,height:.25,up1:1.75,up2:2,down1:1,down2:.75});
});

const storageKey='qh-drawings:v1:CZCE.FGL9:1d';
function savedBox(id) {
  return {id,type:'mm_box',color:'#83acff',points:[{time:'2026-09-01',offset:0,price:100},{time:'2026-09-02',offset:0,price:110}]};
}
function memoryStorage(raw) {
  const values=new Map([[storageKey,raw]]);
  return {values,getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
}
function storageTool(t,storage) {
  const previous=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  Object.defineProperty(globalThis,'localStorage',{value:storage,configurable:true});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'localStorage',previous);else delete globalThis.localStorage;});
  const tool=Object.create(DrawingTools.prototype);
  Object.assign(tool,{items:[],drag:null,messages:[],released:[],refresh(){},lockNavigation(){},notify(message){this.messages.push(message);}});
  tool.element={hasPointerCapture:()=>true,releasePointerCapture:id=>tool.released.push(id)};
  tool.setContext('CZCE.FGL9','1d',[],0);
  return tool;
}

test('a bad or duplicate record does not discard other valid drawings',()=>{
  const good=savedBox('good'),bad={...savedBox('bad'),points:[]};
  const parsed=readDrawingRecord(JSON.stringify({version:1,items:[good,bad,good]}));
  assert.deepEqual(parsed,{items:[good],needsBackup:true});
  assert.deepEqual(readDrawingRecord(null),{items:[],needsBackup:false});
  assert.deepEqual(readDrawingRecord('{broken'),{items:[],needsBackup:true});
  assert.deepEqual(readDrawingRecord(JSON.stringify({version:2,items:[good]})),{items:[],needsBackup:true});
});

test('recovering valid drawings preserves the exact original record before saving',t=>{
  const good=savedBox('good'),raw='\n'+JSON.stringify({version:1,items:[good,{bad:true}]});
  const storage=memoryStorage(raw),tool=storageTool(t,storage);
  assert.deepEqual(tool.items,[good]);assert.equal(storage.values.get(storageKey),raw);assert.equal(storage.values.size,1);
  tool.commit([...tool.items,savedBox('new')]);
  const backup=[...storage.values.entries()].find(([key])=>key.startsWith(storageKey+':recovery:'));
  assert.equal(backup[1],raw);
  assert.deepEqual(JSON.parse(storage.values.get(storageKey)).items.map(x=>x.id),['good','new']);
});

test('a failed recovery backup cannot overwrite the original drawing record',t=>{
  const raw=JSON.stringify({version:1,items:[savedBox('good'),{bad:true}]}),storage=memoryStorage(raw);
  const write=storage.setItem;storage.setItem=(key,value)=>{if(key.includes(':recovery:'))throw new Error('quota');write(key,value);};
  const tool=storageTool(t,storage);tool.commit([...tool.items,savedBox('new')]);
  assert.equal(storage.values.get(storageKey),raw);assert.equal(tool.recoveryRaw,raw);
  storage.setItem=write;assert.equal(tool.save(),true);
  assert.deepEqual(JSON.parse(storage.values.get(storageKey)).items.map(x=>x.id),['good','new']);
});

test('unreadable storage is never overwritten with an empty replacement',t=>{
  const raw=JSON.stringify({version:1,items:[savedBox('good')]}),storage=memoryStorage(raw);
  storage.getItem=()=>{throw new Error('read blocked');};
  const tool=storageTool(t,storage);tool.commit([savedBox('new')]);
  assert.equal(tool.storageReadFailed,true);assert.equal(storage.values.get(storageKey),raw);
});

test('deleting during a drag cannot resurrect the shape on cancel and undo restores the original',t=>{
  const original=savedBox('shape'),storage=memoryStorage(JSON.stringify({version:1,items:[original]}));
  const tool=storageTool(t,storage);tool.selected='shape';
  tool.items=[{...original,points:[{...original.points[0],price:105},original.points[1]]}];
  tool.drag={before:[original],pointerId:7,moved:true};
  tool.remove();tool.cancel();
  assert.deepEqual(tool.items,[]);assert.deepEqual(JSON.parse(storage.values.get(storageKey)).items,[]);
  assert.deepEqual(tool.released,[7]);
  tool.undo();assert.deepEqual(tool.items,[original]);assert.deepEqual(JSON.parse(storage.values.get(storageKey)).items,[original]);
});
