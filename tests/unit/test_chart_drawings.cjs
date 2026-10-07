const test = require('node:test');
const assert = require('node:assert/strict');
const { clipLine, distanceToSegment, channelShift, validDrawing } = require('../../web/drawings.js');

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
