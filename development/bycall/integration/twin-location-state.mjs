/** Renderer-neutral consumer for the host's full location snapshot. No URLs, credentials or network access. */
const factualKey = frame => JSON.stringify(frame.items.map(item=>Object.fromEntries(Object.keys(item).filter(k=>k!=='age_seconds').sort().map(k=>[k,item[k]]))).sort((a,b)=>a.staff.localeCompare(b.staff)||a.watch.localeCompare(b.watch)));

export class TwinLocationState {
  constructor({ monotonicNow = () => performance.now(), wallNow = () => Date.now() } = {}) {
    this.monotonicNow = monotonicNow;
    this.wallNow = wallNow;
    this.frame = null;
    this.retiredEpochs = new Set();
    this.anchors = new Map();
  }

  accept(frame) {
    if (!frame || frame.schema !== 'bycall.location.v1' || frame.delivery !== 'full' ||
        typeof frame.store_id !== 'string' || !frame.store_id ||
        typeof frame.source_epoch !== 'string' || !frame.source_epoch ||
        !Number.isSafeInteger(frame.sequence) || frame.sequence < 1 ||
        typeof frame.revision !== 'string' || !/^[a-f0-9]{64}$/.test(frame.revision) ||
        !Array.isArray(frame.items) || frame.items.length > 500 ||
        !Number.isFinite(Date.parse(frame.server_time)) ||
        !Number.isInteger(frame.consumer_timeout_seconds) || frame.consumer_timeout_seconds < 2 || frame.consumer_timeout_seconds > 30) {
      throw new TypeError('Invalid authoritative location snapshot');
    }
    const staff = new Set(), watches = new Set();
    for (const item of frame.items) {
      if (!item || typeof item.staff !== 'string' || !item.staff || typeof item.watch !== 'string' || !item.watch ||
          staff.has(item.staff) || watches.has(item.watch) || typeof item.online !== 'boolean' ||
          !['fresh', 'unknown', 'stale'].includes(item.location_status) || item.precision !== 'area' || item.coordinates !== null) {
        throw new TypeError('Invalid or duplicate person/device');
      }
      if (item.location_status === 'fresh' &&
          (typeof item.zone_id !== 'string' || !item.zone_id || !Number.isFinite(Date.parse(item.valid_until)))) {
        throw new TypeError('Fresh location requires a zone and deadline');
      }
      if (item.location_status !== 'fresh' && item.zone_id !== null) {
        throw new TypeError('Unknown/stale locations must not carry a live zone');
      }
      staff.add(item.staff); watches.add(item.watch);
    }
    if (this.frame && frame.store_id !== this.frame.store_id) throw new TypeError('Store changed: use a separate feed');
    if (this.retiredEpochs.has(frame.source_epoch)) return false;
    if (this.frame && frame.source_epoch === this.frame.source_epoch) {
      if (frame.sequence < this.frame.sequence) return false;
      if (frame.sequence === this.frame.sequence) {
        if (frame.revision !== this.frame.revision || factualKey(frame) !== factualKey(this.frame)) throw new TypeError('Conflicting revision for the same sequence');
        if (Date.parse(frame.server_time) <= Date.parse(this.frame.server_time)) return false;
      }
    }
    if (this.frame && frame.source_epoch !== this.frame.source_epoch) {
      this.retiredEpochs.add(this.frame.source_epoch);
      this.anchors.clear();
    }
    this.frame = JSON.parse(JSON.stringify(frame));
    this.acceptedAt = this.monotonicNow();
    this.acceptedWall = this.wallNow();
    return true;
  }

  /** anchorMap keys are `${store_id}:${zone_id}`; positions are model anchors, never measured coordinates. */
  project(anchorMap) {
    if (!this.frame) return { connection: 'never_connected', people: [] };
    const elapsed = Math.max(0, this.monotonicNow() - this.acceptedAt, this.wallNow() - this.acceptedWall);
    const lost = elapsed >= this.frame.consumer_timeout_seconds * 1000;
    const serverNow = Date.parse(this.frame.server_time) + elapsed;
    const next = new Map();
    const people = this.frame.items.map(item => {
      let presentation = lost ? 'connection_lost' : !item.online ? 'offline' : item.location_status;
      if (presentation === 'fresh' && serverNow >= Date.parse(item.valid_until)) presentation = 'stale';
      let pose = null, effect = 'hide';
      if (presentation === 'fresh') {
        const anchor = anchorMap.get(`${this.frame.store_id}:${item.zone_id}`);
        if (!anchor || typeof anchor.scene_id !== 'string' || !anchor.scene_id ||
            typeof anchor.anchor_id !== 'string' || !anchor.anchor_id || item.floor == null || anchor.floor !== item.floor ||
            !Array.isArray(anchor.position) || anchor.position.length !== 3 || !anchor.position.every(Number.isFinite)) {
          presentation = 'missing_scene_anchor';
        } else {
          const identity = `${anchor.scene_id}:${anchor.anchor_id}`;
          effect = !this.anchors.has(item.staff) ? 'appear' : this.anchors.get(item.staff) === identity ? 'none' : 'crossfade';
          pose = { type: 'scene_anchor', scene_id: anchor.scene_id, anchor_id: anchor.anchor_id, position: [...anchor.position] };
          next.set(item.staff, identity);
        }
      }
      return { staff: item.staff, watch: item.watch, zone_id: item.zone_id, area: item.area,
               location_status: item.location_status, presentation, pose, effect, precision: 'area' };
    });
    this.anchors = next; // full replacement: removed staff, expired or disconnected markers cannot linger.
    return { connection: lost ? 'lost' : 'live', source_epoch: this.frame.source_epoch,
             sequence: this.frame.sequence, revision: this.frame.revision, people };
  }
}
