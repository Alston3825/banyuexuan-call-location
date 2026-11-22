import test from 'node:test';
import assert from 'node:assert/strict';
import { TwinCallState } from './twin-call-state.mjs';

const rev = c => c.repeat(64);
const frame = (o = {}) => ({
  schema: 'bycall.call.v1', delivery: 'full', store_id: 'example-store', source_epoch: 'e1', sequence: 1,
  revision: rev('a'), server_time: '2026-11-22T10:00:00+00:00', consumer_timeout_seconds: 8,
  items: [{ call_id: 'c1', room: '2414', kind: 'call', state: 'pending', label: '示例呼叫' }], ...o,
});
const clock = () => { let t = 0; return { now: () => t, add: ms => { t += ms; } }; };

test('new call appears, transitions reported once', () => {
  const k = clock(); const s = new TwinCallState({ monotonicNow: k.now, wallNow: k.now });
  assert.equal(s.accept(frame()), true);
  let p = s.project(new Set(['2414']));
  assert.equal(p.calls[0].placement, 'room');
  assert.deepEqual(p.transitions, [{ call_id: 'c1', from: null, to: 'pending' }]);
  assert.deepEqual(s.project().transitions, []);
  s.accept(frame({ sequence: 2, revision: rev('b'), items: [{ call_id: 'c1', room: '2414', kind: 'call', state: 'claimed' }] }));
  p = s.project();
  assert.deepEqual(p.transitions, [{ call_id: 'c1', from: 'pending', to: 'claimed' }]);
});

test('full snapshot removes calls that disappeared', () => {
  const s = new TwinCallState();
  s.accept(frame());
  s.accept(frame({ sequence: 2, revision: rev('b'), items: [] }));
  assert.deepEqual(s.project().calls, []);
});

test('late frames and retired epochs are ignored', () => {
  const s = new TwinCallState();
  s.accept(frame({ sequence: 5, revision: rev('c') }));
  assert.equal(s.accept(frame({ sequence: 4, revision: rev('d') })), false);
  s.accept(frame({ source_epoch: 'e2', sequence: 1, revision: rev('e') }));
  assert.equal(s.accept(frame({ source_epoch: 'e1', sequence: 9, revision: rev('f') })), false);
  assert.equal(s.project().source_epoch, 'e2');
});

test('feed timeout hides every ring', () => {
  const k = clock(); const s = new TwinCallState({ monotonicNow: k.now, wallNow: k.now });
  s.accept(frame());
  k.add(8000);
  const p = s.project();
  assert.equal(p.connection, 'lost');
  assert.deepEqual(p.calls, []);
});

test('unknown room is unplaced, never guessed; bad frames rejected', () => {
  const s = new TwinCallState();
  s.accept(frame({ items: [{ call_id: 'c1', room: '9999', kind: 'call', state: 'pending' }] }));
  assert.equal(s.project(new Set(['2414'])).calls[0].placement, 'unplaced');
  assert.throws(() => s.accept(frame({ schema: 'x' })));
  assert.throws(() => s.accept(frame({ items: [{ call_id: 'a', state: 'weird', room: null }] })));
  assert.throws(() => s.accept(frame({ sequence: 1, revision: rev('z') })));
});
