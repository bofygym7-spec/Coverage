"""GDELT Cloud v2 REST client.

The API key is read from the GDELT_API_KEY environment variable only. It is
never written to disk, never logged, and never embedded in emitted data.

Two API facts drive the whole design and are handled here so callers never
have to think about them:

  * Query windows are capped at 30 inclusive days. Any longer request range is
    split into consecutive chunks and recombined.
  * Coded event history begins 2026-03-01. A request for a period before that
    returns nothing, which is NOT the same as zero events, so the client
    reports the clipped range back to the caller.
"""
from __future__ import annotations
import os, time, logging
from datetime import date, timedelta
from typing import Any, Iterator
import requests

log = logging.getLogger("gdelt")

BASE = os.environ.get("GDELT_API_BASE", "https://gdeltcloud.com/api/v2")
COVERAGE_START = date(2026, 3, 1)      # events; stories begin 2026-03-08
MAX_WINDOW_DAYS = 30          # hard API cap on a single query window
BACKFILL_CHUNK_DAYS = 7       # deliberately below the cap: see search_events
PAGE_LIMIT = 100
MAX_WALK_RESTARTS = 4

DANGER_CATEGORIES = [
    "Battles",
    "Explosions/Remote violence",
    "Violence against civilians",
    "Riots",
    "Strategic developments",
]


class GdeltError(RuntimeError):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"[{code}] {message}")
        self.code, self.status = code, status


class GdeltClient:
    def __init__(self, api_key: str | None = None, timeout: int = 60):
        self.api_key = api_key or os.environ.get("GDELT_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "GDELT_API_KEY is not set. Add it as a repository secret / .env "
                "value. Never place the key in source code."
            )
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "global-security-dashboard/1.0",
        })

    # -- transport ----------------------------------------------------------
    @staticmethod
    def _error_envelope(r) -> tuple[str, str]:
        """Pull (code, message) out of an error response.

        The API returns the code at the TOP level, with `error` a plain string:

            {"success": false, "error": "...", "code": "CURSOR_STALE", "details": {...}}

        An earlier version assumed `error` was a nested object and read
        `error.code`. That raises on a string, a bare except swallowed it, and
        every error arrived labelled HTTP_ERROR. Two behaviours depended on the
        real code and so never ran: the cursor walk never restarted on
        CURSOR_STALE, and an exhausted quota was retried as ordinary throttling.
        Both shapes are accepted now.
        """
        code, msg = "HTTP_ERROR", (r.text or "")[:300]
        try:
            body = r.json()
        except Exception:
            return code, msg
        if not isinstance(body, dict):
            return code, msg
        err = body.get("error")
        if isinstance(err, dict):                  # tolerated alternative shape
            code = err.get("code") or code
            msg = err.get("message") or msg
        elif isinstance(err, str) and err:
            msg = err
        code = body.get("code") or code            # authoritative location
        return code, msg

    def _get(self, path: str, params: dict[str, Any], attempt: int = 0) -> dict:
        r = self.s.get(f"{BASE}{path}", params=params, timeout=self.timeout)

        if r.status_code == 429:
            # Both throttling and quota exhaustion return 429. They need
            # opposite responses, so read the code, not the status.
            code = ""
            try:
                code, _ = self._error_envelope(r)
            except Exception:
                pass
            if code == "QUOTA_EXCEEDED":
                raise GdeltError("QUOTA_EXCEEDED",
                                 "Query-unit quota exhausted. Retrying will not help; "
                                 "the run should stop and resume next cycle.", 429)
            if attempt < 5:
                wait = min(60, 2 ** attempt) + 1
                log.warning("rate limited, sleeping %ss", wait)
                time.sleep(wait)
                return self._get(path, params, attempt + 1)
            raise GdeltError("RATE_LIMITED", "Still throttled after 5 retries.", 429)

        if r.status_code >= 500 and attempt < 4:
            time.sleep(2 ** attempt)
            return self._get(path, params, attempt + 1)

        if not r.ok:
            code, msg = self._error_envelope(r)
            raise GdeltError(code, msg, r.status_code)

        return r.json()

    # -- windows ------------------------------------------------------------
    @staticmethod
    def split_window(start: date, end: date,
                     chunk_days: int = MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
        """Break any range into consecutive chunks, clipped to coverage.

        chunk_days defaults to the API cap but callers doing long backfills pass
        something smaller. A short window means few pages per cursor walk, so the
        walk is likely to finish before GDELT next rebuilds its snapshot, and a
        restart costs little when it does not.
        """
        chunk_days = max(1, min(chunk_days, MAX_WINDOW_DAYS))
        start = max(start, COVERAGE_START)
        if end < start:
            return []
        out, cur = [], start
        while cur <= end:
            stop = min(cur + timedelta(days=chunk_days - 1), end)
            out.append((cur, stop))
            cur = stop + timedelta(days=1)
        return out

    @staticmethod
    def coverage_note(requested_start: date) -> str | None:
        if requested_start < COVERAGE_START:
            return (f"Requested period begins {requested_start}, before GDELT coded "
                    f"coverage starts ({COVERAGE_START}). The earlier span is reported "
                    f"as NO COVERAGE, not as zero events.")
        return None

    # -- events -------------------------------------------------------------
    def search_stories(self, start: date, end: date,
                       chunk_days: int = MAX_WINDOW_DAYS, **filters) -> list[dict]:
        """Walk narrative stories. Same windowing and restart rules as events."""
        return list(self._walk("/stories", start, end, chunk_days, filters))

    def search_events(self, start: date, end: date, chunk_days: int = MAX_WINDOW_DAYS,
                      **filters) -> Iterator[dict]:
        """Yield every event in [start, end], splitting windows and paging cursors.

        GDELT periodically rebuilds the snapshot a cursor was issued against and
        returns CURSOR_STALE. The cursor cannot be resumed: the only consistent
        response is to walk that window again from the first page. Backfills run
        long enough to cross a rebuild routinely, so this is expected operation
        rather than an error.

        Restarting re-yields events already seen. That is safe because ingest
        upserts on gdelt_event_id, so a repeated event updates in place and
        first_seen_at is preserved.
        """
        yield from self._walk("/events", start, end, chunk_days, filters)

    def _walk(self, path: str, start: date, end: date, chunk_days: int,
              filters: dict) -> Iterator[dict]:
        for w_start, w_end in self.split_window(start, end, chunk_days):
            for attempt in range(MAX_WALK_RESTARTS + 1):
                cursor, yielded = None, 0
                try:
                    while True:
                        params = {
                            "start_date": w_start.isoformat(),
                            "end_date": w_end.isoformat(),
                            "limit": PAGE_LIMIT,
                            "sort": "recent",
                            **{k: v for k, v in filters.items() if v is not None},
                        }
                        if isinstance(params.get("category"), (list, tuple)):
                            params["category"] = ",".join(params["category"])
                        if cursor:
                            params["cursor"] = cursor
                        payload = self._get(path, params)
                        for row in payload.get("data", []):
                            yielded += 1
                            yield row
                        cursor = (payload.get("pagination") or {}).get("next_cursor")
                        if not cursor:
                            break
                    break                      # window walked cleanly
                except GdeltError as e:
                    if e.code != "CURSOR_STALE" or attempt == MAX_WALK_RESTARTS:
                        raise
                    log.warning("snapshot rebuilt %s..%s after %d events; "
                                "restarting walk (attempt %d/%d)",
                                w_start, w_end, yielded, attempt + 1, MAX_WALK_RESTARTS)
                    time.sleep(2)

    def summarize_events(self, start: date, end: date, group_by: str = "country", **filters) -> list[dict]:
        out: list[dict] = []
        for w_start, w_end in self.split_window(start, end):
            params = {
                "start_date": w_start.isoformat(),
                "end_date": w_end.isoformat(),
                "group_by": group_by,
                "limit": 500,
                **{k: v for k, v in filters.items() if v is not None},
            }
            if isinstance(params.get("category"), (list, tuple)):
                params["category"] = ",".join(params["category"])
            out.extend(self._get("/events/summary", params).get("data", []))
        return out

    def latest_event_date(self) -> str | None:
        today = date.today()
        rows = self.summarize_events(today - timedelta(days=7), today, group_by="date")
        keys = sorted(r["key"] for r in rows if r.get("count"))
        return keys[-1] if keys else None
