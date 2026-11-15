"""Message protocol (dev plan §四). All devices speak this envelope over MQTT."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# message types
CALL_REQUEST = 'CALL_REQUEST'      # room -> host
CALL_DISPATCH = 'CALL_DISPATCH'    # host -> watch / station
CALL_TAKEN = 'CALL_TAKEN'          # host -> other notified watches
CALL_STATE = 'CALL_STATE'          # host -> owner watch (arrived / completed)
CALL_ACCEPT = 'CALL_ACCEPT'        # watch -> host
CALL_ARRIVE = 'CALL_ARRIVE'        # watch -> host (manual arrival)
CALL_COMPLETE = 'CALL_COMPLETE'    # watch -> host
CALL_CANCEL = 'CALL_CANCEL'        # room/watch -> host
CALL_TRANSFER = 'CALL_TRANSFER'    # watch -> host
CALL_FEEDBACK = 'CALL_FEEDBACK'    # host -> room tablet
CMD_REJECTED = 'CMD_REJECTED'      # host -> sender
HEARTBEAT = 'HEARTBEAT'
LOCATION_REPORT = 'LOCATION_REPORT'
VITALS_REPORT = 'VITALS_REPORT'    # watch -> host (heart rate etc.), local only
DEVICE_CONFIG = 'DEVICE_CONFIG'  # host -> room tablet (button list), on topic reply/{dev}
STATUS = 'STATUS'                  # LWT / online marker

FEEDBACK_TEXT = {
    'received': '已接到呼叫，请稍后',
    'accepted': '{nurse}已接单，马上过来',
    'arrived': '护理师已到达',
    'completed': '已完成',
    'cancelled': '呼叫已取消',
}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class Envelope(BaseModel):
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    call_id: Optional[str] = None
    store_id: str
    device_id: str
    timestamp: str = Field(default_factory=_now_iso)
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator('store_id', 'device_id', 'msg_id', 'type')
    @classmethod
    def _nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError('must be non-empty')
        return v


class Reading(BaseModel):
    beacon_id: str
    rssi: int = Field(ge=-127, le=0)


class LocationPayload(BaseModel):
    readings: List[Reading]


class VitalsPayload(BaseModel):
    heart_rate: Optional[int] = Field(default=None, ge=20, le=250)
    spo2: Optional[int] = Field(default=None, ge=50, le=100)
    temperature: Optional[float] = Field(default=None, ge=30, le=45)
    steps: Optional[int] = Field(default=None, ge=0)
    measured_at: Optional[str] = None


class RequestPayload(BaseModel):
    room: str                     # room_id (management-system cuid)
    service_code: str             # e.g. NEED_NURSE / EMERGENCY, see service-codes
    priority: str = 'normal'
    voice_url: Optional[str] = None
    text: Optional[str] = None


class HeartbeatPayload(BaseModel):
    battery: Optional[int] = Field(default=None, ge=0, le=100)
    wifi_rssi: Optional[int] = None
    charging: Optional[bool] = None    # watches: currently on the charger
    battery_sample: bool = False
    cfg_ver: Optional[str] = None      # tablets: version of the button list they hold


# topics --------------------------------------------------------------
def t_call(store, floor, room): return f'{store}/call/{floor}/{room}'
def t_dispatch_watch(store, watch): return f'{store}/dispatch/{watch}'
def t_dispatch_station(store): return f'{store}/station/dispatch'
def t_ack(store, call_id): return f'{store}/ack/{call_id}'
def t_feedback(store, room): return f'{store}/feedback/{room}'
def t_heartbeat(store, dev): return f'{store}/heartbeat/{dev}'
def t_location(store, watch): return f'{store}/location/{watch}'
def t_vitals(store, watch): return f'{store}/vitals/{watch}'
def t_status(store, dev): return f'{store}/status/{dev}'
def t_events(store): return f'{store}/call/events'
def t_reply(store, dev): return f'{store}/reply/{dev}'


def host_subscriptions(store):
    return [f'{store}/call/+/+', f'{store}/ack/+', f'{store}/heartbeat/+',
            f'{store}/location/+', f'{store}/vitals/+', f'{store}/status/+', f'{store}/watch/+']
