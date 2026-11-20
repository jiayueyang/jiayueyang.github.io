import assert from 'node:assert/strict';
import test from 'node:test';
import { groupJump, paperTrack, seekTarget } from '../web/player.js';

const paper={sections:[
  {id:'intro',title:'Setup',paragraphs:[{id:'p1',sentences:[
    {id:'a',en:'Setup.',zh:'设置。'},
    {id:'b',en:'{{eq:e1}}',zh:'{{eq:e1}}'},
  ]},{id:'p2',sentences:[{id:'c',en:'A new paragraph.',zh:'下一段。'}]}]},
  {id:'result',title:'Results',paragraphs:[{id:'p3',sentences:[{id:'d',en:'Results.',zh:''}]}]},
]};
test('document order, translations pending and formula modes',()=>{
  assert.deepEqual(paperTrack(paper,'zh').map(x=>x.id),['a','b','c','d']);
  assert.deepEqual(paperTrack(paper,'zh','skip').map(x=>x.id),['a','c','d']);
  const track=paperTrack(paper,'en');
  assert.deepEqual([groupJump(track,0,1,'paragraphId'),groupJump(track,3,-1,'sectionId')],[2,0]);
});
test('known real durations drive time-based seeking even for very unequal sentence durations',()=>{
  assert.deepEqual(seekTarget([4,12,4],.5),{index:1,time:6,fraction:.5});
  assert.deepEqual(seekTarget([4,12,4],.8),{index:2,time:0,fraction:0});
  assert.deepEqual(seekTarget([4,12,4],1),{index:2,time:4,fraction:1});
});
test('unknown duration seeks by sentence and waits for metadata to seek within it',()=>{
  assert.deepEqual(seekTarget([1,undefined,10],.5),{index:1,time:0,fraction:.5});
  assert.deepEqual(seekTarget([],1),{index:0,time:0,fraction:0});
});
