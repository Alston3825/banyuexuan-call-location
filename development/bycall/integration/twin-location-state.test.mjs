import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TwinLocationState } from './twin-location-state.mjs';

const timestamp = '2026-11-20T10:00:00Z';
const baseTime = Date.parse(timestamp);
const person = (overrides = {}) => ({ staff: 'n01', watch: 'w01', online: true, location_status: 'fresh',
  precision: 'area', coordinates: null, zone_id: '24-1', area: '示例区域', floor: '24F',
  valid_until: new Date(baseTime + 30000).toISOString(), ...overrides });
const frame = (overrides = {}) => ({ schema: 'bycall.location.v1', delivery: 'full', store_id: 'test',
  source_epoch: 'boot-a', sequence: 1, revision: 'a'.repeat(64), server_time: timestamp,
  consumer_timeout_seconds: 8, items: [person()], ...overrides });
const anchors = () => new Map([
  ['test:24-1', { scene_id: 'scene-test', anchor_id: 'anchor-a', floor: '24F', position: [10, 0, 5] }],
  ['test:24-2', { scene_id: 'scene-test', anchor_id: 'anchor-b', floor: '24F', position: [15, 0, 8] }],
]);
function consumer() { let time = 0; return { client: new TwinLocationState({monotonicNow:()=>time,wallNow:()=>time}), tick:ms=>{time+=ms;} }; }

test('fresh positions use configured model anchors, not invented measured coordinates', () => {
  const {client}=consumer(); client.accept(frame());
  const p=client.project(anchors()).people[0];
  assert.equal(p.presentation,'fresh'); assert.equal(p.pose.type,'scene_anchor'); assert.equal(p.effect,'appear');
});
test('swapping a watch retains one avatar keyed by employee',()=>{
  const {client}=consumer(); client.accept(frame()); client.project(anchors());
  client.accept(frame({sequence:2,revision:'b'.repeat(64),items:[person({watch:'spare-01'})]}));
  const result=client.project(anchors()); assert.equal(result.people.length,1); assert.equal(result.people[0].staff,'n01'); assert.equal(result.people[0].effect,'none');
});
test('changing zones produces a visual crossfade, not an asserted walking path',()=>{
  const {client}=consumer(); client.accept(frame()); client.project(anchors());
  client.accept(frame({sequence:2,revision:'b'.repeat(64),items:[person({zone_id:'24-2'})]}));
  assert.equal(client.project(anchors()).people[0].effect,'crossfade');
});
test('full snapshots remove people, unknown locations stop drawing the old pose',()=>{
  const {client}=consumer();client.accept(frame());client.project(anchors());
  client.accept(frame({sequence:2,revision:'b'.repeat(64),items:[person({location_status:'unknown',zone_id:null,valid_until:null})]}));
  assert.equal(client.project(anchors()).people[0].pose,null);
  client.accept(frame({sequence:3,revision:'c'.repeat(64),items:[]}));assert.equal(client.project(anchors()).people.length,0);
});
test('network silence clears live poses at eight seconds even if source TTL is longer',()=>{
  const {client,tick}=consumer();client.accept(frame());tick(8000);
  const result=client.project(anchors());assert.equal(result.connection,'lost');assert.equal(result.people[0].pose,null);
});
test('expiry is based on source deadline and elapsed time, not polling success',()=>{
  const {client,tick}=consumer();client.accept(frame({items:[person({valid_until:new Date(baseTime+3000).toISOString()})]}));
  tick(3000);assert.equal(client.project(anchors()).people[0].presentation,'stale');
});
test('out-of-order data, replay and retired process epochs cannot rewind a scene',()=>{
  const {client,tick}=consumer();client.accept(frame({sequence:2}));
  assert.equal(client.accept(frame({sequence:1})),false);
  assert.equal(client.accept(frame({sequence:2})),false);
  client.accept(frame({source_epoch:'boot-b',sequence:1}));
  assert.equal(client.accept(frame({source_epoch:'boot-a',sequence:3})),false);
  tick(8000);assert.equal(client.project(anchors()).connection,'lost');
});
test('wrong-floor or missing mappings never fall back to scene origin',()=>{
  const {client}=consumer();client.accept(frame());
  assert.equal(client.project(new Map()).people[0].presentation,'missing_scene_anchor');
  const map=anchors();map.get('test:24-1').floor='16F';assert.equal(client.project(map).people[0].pose,null);
});
test('duplicate people and same-sequence conflicts are rejected',()=>{
  const {client}=consumer();assert.throws(()=>client.accept(frame({items:[person(),person({watch:'w02'})]})));
  client.accept(frame());assert.throws(()=>client.accept(frame({revision:'b'.repeat(64)})));
  assert.throws(()=>client.accept(frame({server_time:new Date(baseTime+1000).toISOString(),items:[person({zone_id:'24-2'})]})));
});
