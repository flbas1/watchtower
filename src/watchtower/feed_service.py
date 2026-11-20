"""The feed service: runs every feed on its own schedule, on background threads.

The contract with feeds.py (the feed readers' side):
  - a feed has `name`, `update_minutes`, and `fetch(client) -> DataFrame | None`;
  - `fetch` returns a DataFrame of items (title, url, source, time, lat, lon),
    or None when nothing changed (e.g. an ETag 304);
  - `fetch` just raises on errors; this module catches them.

Stories whose title contains a `skip_keywords` entry are dropped here, so nothing
downstream ever sees them.

This module never parses anything. It decides *when* to fetch, does it on a background
thread, keeps the latest DataFrame per feed, and tells listeners (plain callbacks).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

if TYPE_CHECKING:            # only for type hints; feeds.py is not imported at run time
    from .feeds import Feed

USER_AGENT = "watchtower (personal open-source intelligence app)"   # every request the app makes says who it is
SHARE_SECONDS = 60       # feeds asking for the same URL this close together share one download


class SharedDownloads:
    """Stands in for the httpx client: feeds that GET the same URL (with the same headers and no
    login) within SHARE_SECONDS share one download, e.g. the three NHC feeds all reading
    CurrentStorms.json. A feed asking while that download is still running waits for it.
    Requests with a login (auth=...) are never shared.

    `pace` {site: seconds}: requests to that site (e.g. "www.reddit.com") go one at a time,
    at least that many seconds apart, so a site that limits how fast you ask (Reddit answers
    "429 Too Many Requests") isn't asked by many feeds at once."""

    def __init__(self, client: httpx.Client, pace: dict[str, float] | None = None) -> None:
        self._client = client
        self._lock = threading.Lock()                  # guards _entries
        self._entries: dict[tuple, list] = {}          # (url, headers) -> [its own lock, response, when]
        self._pace = {host.lower(): float(seconds) for host, seconds in (pace or {}).items()}
        self._turns = {host: [threading.Lock(), 0.0] for host in self._pace}   # host -> [lock, last request]

    def _request(self, url, headers, **kwargs) -> httpx.Response:
        host = httpx.URL(str(url)).host.lower()
        if host not in self._turns:
            return self._client.get(url, headers=headers, **kwargs)
        turn = self._turns[host]
        with turn[0]:                                  # this site's requests, one at a time
            wait = turn[1] + self._pace[host] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                return self._client.get(url, headers=headers, **kwargs)
            finally:
                turn[1] = time.monotonic()

    def get(self, url, headers: dict | None = None, **kwargs) -> httpx.Response:
        if kwargs:
            return self._request(url, headers, **kwargs)
        key = (str(url), tuple(sorted((headers or {}).items())))
        now = time.monotonic()
        with self._lock:
            for old in [k for k, e in self._entries.items() if e[1] is not None and now - e[2] >= SHARE_SECONDS]:
                del self._entries[old]                 # forget expired downloads
            entry = self._entries.setdefault(key, [threading.Lock(), None, 0.0])
        with entry[0]:                                 # one download per URL at a time
            if entry[1] is not None and time.monotonic() - entry[2] < SHARE_SECONDS:
                return entry[1]
            response = self._request(url, headers)
            entry[1], entry[2] = response, time.monotonic()
            return response


def skip_stories(data: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    """`data` without the stories whose title contains any keyword (any case)."""
    words = [str(k).casefold() for k in keywords if str(k).strip()]
    if not words or data.empty or "title" not in data.columns:
        return data
    titles = data["title"].fillna("").astype(str).str.casefold()
    skip = pd.Series(False, index=data.index)
    for word in words:
        skip |= titles.str.contains(word, regex=False)
    return data[~skip].reset_index(drop=True)


class FeedService:
    """Fetches every feed now, then each one every `update_minutes`, on a few worker threads.
    One scheduler thread wakes when the next feed is due. Listeners added with
    `on_update(fn)` / `on_failure(fn)` are called on the worker thread that fetched, so they
    must be quick and thread-safe (the news writer only starts a timer).

    A feed that fails tries again later: after retry `first_minutes` (default 15), then twice
    as long each time it fails again, at most `max_minutes` (default 240) apart. Meanwhile
    failed() says why and when it tries again; once it works, it's back on its own schedule."""

    WORKERS = 4

    def __init__(self, skip_keywords: list[str] | None = None, pace: dict[str, float] | None = None,
                 retry: dict | None = None) -> None:
        """`skip_keywords`: stories whose title contains one of these are dropped from every feed.
        `pace`: {site: seconds} between requests to that site. `retry`: {first_minutes, max_minutes}."""
        self._skip_keywords = list(skip_keywords or [])
        self._retry_first = float((retry or {}).get("first_minutes", 15))
        self._retry_max = float((retry or {}).get("max_minutes", 240))
        self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
        self._downloads = SharedDownloads(self._client, pace)
        self._tries: dict[str, int] = {}               # name -> failures in a row
        self._pool = ThreadPoolExecutor(max_workers=self.WORKERS, thread_name_prefix="feed")
        self._lock = threading.Lock()                  # guards everything below
        self._feeds: dict[str, Feed] = {}
        self._due: dict[str, float] = {}               # name -> time.monotonic() when next due
        self._latest: dict[str, pd.DataFrame] = {}
        self._fetched_at: dict[str, datetime] = {}
        self._running: set[str] = set()                # feeds with a fetch in progress
        self._failed: dict[str, str] = {}              # name -> reason
        self._updated_listeners, self._failed_listeners = [], []
        self._wake = threading.Event()
        self._stopping = False
        self._scheduler: threading.Thread | None = None

    # --- setup ------------------------------------------------------------

    def add(self, feed: Feed) -> None:
        """Register a feed. It starts fetching when start() is called."""
        self._feeds[feed.name] = feed

    def on_update(self, listener) -> None:
        """listener(name): new data is available via latest(name)."""
        self._updated_listeners.append(listener)

    def on_failure(self, listener) -> None:
        """listener(name, reason): the feed failed (it tries again later)."""
        self._failed_listeners.append(listener)

    def start(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._due = {name: now for name in self._feeds}
        self._scheduler = threading.Thread(target=self._schedule, name="feed-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._client.close()

    # --- fetching ---------------------------------------------------------

    def _schedule(self) -> None:
        """Hand each due feed to a worker, then sleep until the next one is due."""
        while not self._stopping:
            now = time.monotonic()
            with self._lock:
                due = [n for n, t in self._due.items() if t <= now and n not in self._running]
                for name in due:
                    self._running.add(name)
                    self._due[name] = now + self._feeds[name].update_minutes * 60   # a failure moves it to the retry time
                waiting = list(self._due.values())
            for name in due:
                self._pool.submit(self._fetch, name)
            self._wake.wait(max(0.5, min(waiting, default=now + 60) - time.monotonic()))
            self._wake.clear()

    def refresh(self, name: str) -> None:
        """Fetch one feed as soon as possible."""
        with self._lock:
            if name in self._due:
                self._due[name] = 0
        self._wake.set()

    def _fetch(self, name: str) -> None:
        try:
            data = self._feeds[name].fetch(self._downloads)
            error = ""
        except Exception as ex:                        # a feed's problem never stops the others
            data, error = None, f"{type(ex).__name__}: {ex}"
        with self._lock:
            self._running.discard(name)
            recovered = False
            if error:
                tries = self._tries[name] = self._tries.get(name, 0) + 1
                minutes = min(self._retry_first * 2 ** (tries - 1), self._retry_max)
                self._due[name] = time.monotonic() + minutes * 60
                again = datetime.now().astimezone() + pd.Timedelta(minutes=minutes)
                error = f"{error} (trying again at {again:%H:%M})"
                self._failed[name] = error
            else:
                recovered = self._failed.pop(name, None) is not None
                self._tries.pop(name, None)
                self._fetched_at[name] = datetime.now(timezone.utc)
                if data is not None:                   # None: nothing changed (e.g. 304)
                    self._latest[name] = skip_stories(data, self._skip_keywords)   # replace the whole DataFrame, never edit it
        if error:
            self._wake.set()                           # the scheduler may be asleep until later than the retry
            print(f"feed '{name}' failed: {error}")
            for listener in self._failed_listeners:
                listener(name, error)
        elif data is not None or recovered:            # a recovered feed is no longer greyed out on the page
            for listener in self._updated_listeners:
                listener(name)

    # --- reading ----------------------------------------------------------

    def latest(self, name: str) -> pd.DataFrame | None:
        """The newest items for a feed, or None if it has not delivered any yet."""
        with self._lock:
            return self._latest.get(name)

    def fetched_at(self, name: str) -> datetime | None:
        """When the feed last answered successfully (including 'nothing changed')."""
        with self._lock:
            return self._fetched_at.get(name)

    def failed(self) -> dict[str, str]:
        """Feeds whose last try failed, with the reason and when they try again."""
        with self._lock:
            return dict(self._failed)
