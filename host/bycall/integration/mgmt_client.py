"""Client for the management system integration API (集成规范-呼叫定位系统 §2–§5).

base_url is the API root that contains `/integration/v1/*`, e.g. https://mgmt.example/api/v1.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import httpx

PREFIX = '/integration/v1'


class MgmtError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class MgmtClient:
    def __init__(self, base_url, api_key, transport=None, timeout=10.0):
        self.http = httpx.Client(base_url=base_url.rstrip('/'), timeout=timeout, transport=transport,
                                 headers={'Authorization': f'Bearer {api_key}'})

    def _req(self, method, path, **kw):
        try:
            r = self.http.request(method, PREFIX + path, **kw)
        except httpx.HTTPError as e:
            raise MgmtError(f'network: {e}')
        if r.status_code >= 400:
            raise MgmtError(f'{r.status_code}: {r.text[:200]}', r.status_code)
        return r.json()

    def ping(self):
        return self._req('GET', '/ping')

    def pull(self, resource: str, store_id: Optional[str] = None, updated_since: Optional[str] = None,
             **extra) -> Tuple[List[dict], Optional[str]]:
        """Incremental list pull; follows hasMore/nextSince. Returns (items, new_cursor)."""
        items, since = [], updated_since
        seen = {since}
        while True:
            params: Dict[str, str] = {k: v for k, v in extra.items() if v}
            if store_id:
                params['storeId'] = store_id
            if since:
                params['updatedSince'] = since
            page = self._req('GET', f'/{resource}', params=params)
            items += page.get('items', [])
            nxt = page.get('nextSince') or since
            if not page.get('hasMore'):
                return items, nxt
            if nxt in seen:
                raise MgmtError('pagination stalled: nextSince repeated')
            seen.add(nxt)
            since = nxt

    def events(self, store_id, after_seq=None):
        items, seq = [], after_seq
        seen = {seq}
        while True:
            params = {'storeId': store_id}
            if seq is not None:
                params['afterSeq'] = str(seq)
            page = self._req('GET', '/events', params=params)
            items += page.get('items', [])
            nxt = page.get('nextSeq', seq)
            if not page.get('hasMore'):
                return items, nxt
            if nxt is None or nxt in seen:
                raise MgmtError('pagination stalled: nextSeq missing or repeated')
            seen.add(nxt)
            seq = nxt

    def post_call_event(self, record: dict):
        return self._req('POST', '/call-events', json=record)

    def verify(self, username, password) -> dict:
        return self._req('POST', '/auth/verify', json=dict(username=username, password=password))
