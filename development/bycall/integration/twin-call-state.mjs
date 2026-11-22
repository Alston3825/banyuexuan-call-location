/** Renderer-neutral consumer for the host's full call snapshot (schema bycall.call.v1).
 *  No URLs, credentials or network access. The host alone decides call state: this module
 *  never derives "arrived" or "completed" from a person's location. */
const STATES = ['pending', 'claimed', 'arrived', 'completed', 'cancelled'];

export class TwinCallState {
  constructor({ monotonicNow = () => performance.now(), wallNow = () => Date.now() } = {}) {
    this.monotonicNow = monotonicNow;
    this.wallNow = wallNow;
    this.frame = null;
    this.retiredEpochs = new Set();
    this.seen = new Map(); // call_id -> last state, to report transitions once
  }

  accept(frame) {
    if (!frame || frame.schema !== 'bycall.call.v1' || frame.delivery !== 'full' ||
        typeof frame.store_id !== 'string' || !frame.store_id ||
        typeof frame.source_epoch !== 'string' || !frame.source_epoch ||
        !Number.isSafeInteger(frame.sequence) || frame.sequence < 1 ||
        typeof frame.revision !== 'string' || !/^[a-f0-9]{64}$/.test(frame.revision) ||
        !Array.isArray(frame.items) || frame.items.length > 1000 ||
        !Number.isFinite(Date.parse(frame.server_time)) ||
        !Number.isInteger(frame.consumer_timeout_seconds) || frame.consumer_timeout_seconds < 2 || frame.consumer_timeout_seconds > 30) {
      throw new TypeError('Invalid authoritative call snapshot');
    }
    const ids = new Set();
    for (const c of frame.items) {
      if (!c || typeof c.call_id !== 'string' || !c.call_id || ids.has(c.call_id) ||
          !STATES.includes(c.state) || (c.room !== null && typeof c.room !== 'string')) {
        throw new TypeError('Invalid or duplicate call');
      }
      ids.add(c.call_id);
    }
    if (this.frame && frame.store_id !== this.frame.store_id) throw new TypeError('Store changed: use a separate feed');
    if (this.retiredEpochs.has(frame.source_epoch)) return false;
    if (this.frame && frame.source_epoch === this.frame.source_epoch) {
      if (frame.sequence < this.frame.sequence) return false;
      if (frame.sequence === this.frame.sequence) {
        if (frame.revision !== this.frame.revision) throw new TypeError('Conflicting revision for the same sequence');
        if (Date.parse(frame.server_time) <= Date.parse(this.frame.server_time)) return false;
      }
    }
    if (this.frame && frame.source_epoch !== this.frame.source_epoch) {
      this.retiredEpochs.add(this.frame.source_epoch);
    }
    this.frame = JSON.parse(JSON.stringify(frame));
    this.acceptedAt = this.monotonicNow();
    this.acceptedWall = this.wallNow();
    return true;
  }

  /** roomSet: rooms that exist in the 3D model. Returns calls to draw and state transitions to log. */
  project(roomSet = null) {
    if (!this.frame) return { connection: 'never_connected', calls: [], transitions: [] };
    const elapsed = Math.max(0, this.monotonicNow() - this.acceptedAt, this.wallNow() - this.acceptedWall);
    if (elapsed >= this.frame.consumer_timeout_seconds * 1000) {
      // Never keep showing rings as live once the feed is lost.
      return { connection: 'lost', calls: [], transitions: [] };
    }
    const transitions = [];
    const next = new Map();
    const calls = this.frame.items.map(c => {
      const before = this.seen.get(c.call_id);
      if (before !== c.state) transitions.push({ call_id: c.call_id, from: before ?? null, to: c.state });
      next.set(c.call_id, c.state);
      const placement = c.room && (!roomSet || roomSet.has(c.room)) ? 'room' : c.kind === 'help' ? 'person' : 'unplaced';
      return { ...c, placement, live: ['pending', 'claimed', 'arrived'].includes(c.state) };
    });
    this.seen = next;
    return { connection: 'live', source_epoch: this.frame.source_epoch, sequence: this.frame.sequence, calls, transitions };
  }
}
