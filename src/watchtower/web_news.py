"""Writes the latest stories and places of the feeds for the web page: news.js (marquees),
geo.js (maps) and layers/<feed>.png (pictures laid on the maps), in the web folder.

The files are JavaScript (`window.NEWS = {...};`) so the page also works opened straight
from disk, where it can't read files with fetch() but can load a script.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import icons
from .feed_service import FeedService

STORIES_PER_FEED = 20
WRITE_DELAY_MS = 5000        # feeds arriving close together are written once


# Formats whose items read well as headlines go into news.js (marquees) unless a feed says
# `marquee: false`; any other feed can join with `marquee: true`.
MARQUEE_FORMATS = {"rss", "nws_alerts", "iem_storm_reports", "gdelt_events", "faa_tfr", "faa_airport_status",
                   "cisa_kev", "ioda_outages", "celestrak_satellites", "gpsjam"}
NO_PLACE_FORMATS = {"rss", "cisa_kev"}          # never on a map
KEEP_ORDER_FORMATS = {"celestrak_satellites", "gpsjam", "ioda_outages"}   # the reader's order (soonest pass, worst first), not newest first


def web_feeds(cfg: dict) -> set[str]:
    """The feeds osint-web's marquees can show: MARQUEE_FORMATS, or a feed's own `marquee`."""
    return {name for name, f in cfg["feed_catalog"].items()
            if f.get("marquee", f.get("format") in MARQUEE_FORMATS)}


def geo_feeds(cfg: dict) -> set[str]:
    """The feeds osint-web's maps can show: every feed that has places (storms, quakes, the sun...)."""
    return {name for name, f in cfg["feed_catalog"].items() if f.get("format") and f.get("format") not in NO_PLACE_FORMATS}


def _number(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not pd.isna(x) else None


def geo_object(cfg: dict, service: FeedService, names: set[str]) -> dict:
    """{"updated", "location": {"lat", "lon"}, "feeds": {key: {"name", "format", "failed",
    "items": [{"title", "url", "source", "time", "lat", "lon", "magnitude", "details", "id", "heading",
    "emergency", "geometry"}]}}}.
    `geometry` is [[kind, [[lat, lon], ...]], ...] with kind "line", "polygon" or "point"."""
    feeds = {}
    failed = service.failed()
    for key in sorted(names, key=lambda k: cfg["feed_catalog"][k]["name"].casefold()):
        settings = cfg["feed_catalog"][key]
        feeds[key] = {"name": settings["name"], "format": settings["format"], "failed": failed.get(key), "items": [],
                      "rain": settings.get("rain_layer")}       # a number: drawn under the page's Rain layer, lowest first
        items = service.latest(key)
        if items is None or items.empty:
            continue
        for row in items.to_dict("records"):
            when = row.get("time")
            geometry = row.get("geometry") if isinstance(row.get("geometry"), list) else []
            feeds[key]["items"].append({
                "title": row.get("title") if isinstance(row.get("title"), str) else "",
                "url": row.get("url") if isinstance(row.get("url"), str) else None,
                "source": row.get("source") if isinstance(row.get("source"), str) else settings["name"],
                "time": when.isoformat() if isinstance(when, pd.Timestamp) and not pd.isna(when) else None,
                "lat": _number(row.get("lat")), "lon": _number(row.get("lon")),
                "magnitude": _number(row.get("magnitude")),
                "details": row.get("details") if isinstance(row.get("details"), str) else "",
                "id": str(row["id"]) if isinstance(row.get("id"), (str, int)) else None,   # the same thing across updates (a plane's hex)
                "heading": _number(row.get("heading")),          # flights: which way it's pointing
                "emergency": bool(row.get("emergency")) if not pd.isna(row.get("emergency", False)) else False,
                "flags": row.get("flags") if isinstance(row.get("flags"), str) else "",   # flights: "military interesting pia ladd"
                "geometry": [[kind, [[float(lat), float(lon)] for lat, lon in points]] for kind, points in geometry],
            })
            if isinstance(row.get("tiles"), str):                   # map tiles the page loads itself (global rain)
                feeds[key]["items"][-1]["tiles"] = row["tiles"]
            if isinstance(row.get("image"), bytes):                 # a picture to lay on the map (rain radar)
                version = str(row.get("id") or row.get("time"))
                feeds[key]["items"][-1]["image"] = f"{IMAGE_FOLDER}/{key}.png?v={version}"   # ?v: a new one is loaded, not the cached
                feeds[key]["items"][-1]["bounds"] = [float(b) for b in row["bounds"]]
    location = cfg.get("location") or {}
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "location": {"lat": location.get("lat"), "lon": location.get("lon")}, "feeds": feeds}


IMAGE_FOLDER = "layers"        # next to geo.js: pictures laid on the maps (rain radar)


def write_images(service: FeedService, names: set[str], folder: Path) -> None:
    """Save each feed's picture (a row with `image` bytes) as <folder>/<key>.png, via a
    temporary file so the page never loads half of one."""
    for key in names:
        items = service.latest(key)
        if items is None or items.empty or "image" not in items.columns:
            continue
        image = items["image"].iloc[0]
        if not isinstance(image, bytes):
            continue
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f"{key}.png.tmp"
        temp.write_bytes(image)
        os.replace(temp, folder / f"{key}.png")


def news_object(cfg: dict, service: FeedService, names: set[str], with_icon: set[str] = frozenset()) -> dict:
    """{"updated": ..., "feeds": {key: {"name", "icon", "failed", "stories": [{"title", "url", "time"}]}}}:
    every feed in `names`, sorted by name, newest stories first. A feed that hasn't delivered
    has no stories; a feed that stopped says why in "failed"; "icon" is "icons/<key>.png" or None."""
    feeds = {}
    failed = service.failed()
    for key in sorted(names, key=lambda k: cfg["feed_catalog"][k]["name"].casefold()):
        feeds[key] = {"name": cfg["feed_catalog"][key]["name"],
                      "icon": f"icons/{key}.png" if key in with_icon else None,
                      "failed": failed.get(key),
                      "stories": []}
        items = service.latest(key)
        if items is None or items.empty:
            continue
        if "time" in items.columns and cfg["feed_catalog"][key].get("format") not in KEEP_ORDER_FORMATS:
            items = items.sort_values("time", ascending=False, na_position="last", kind="stable")
        stories = []
        for row in items.head(STORIES_PER_FEED).itertuples():
            title = getattr(row, "title", None)
            if not isinstance(title, str) or not title.strip():
                continue
            url = getattr(row, "url", None)
            when = getattr(row, "time", None)
            stories.append({"title": title.strip(),
                            "url": url if isinstance(url, str) else None,
                            "time": when.isoformat() if isinstance(when, pd.Timestamp) and not pd.isna(when) else None})
        feeds[key]["stories"] = stories
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feeds": feeds}


def write_news(path: Path, news: dict, variable: str = "NEWS") -> None:
    """Write `window.<variable> = {...};` via a temporary file, so the page never loads a
    half-written one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(f"window.{variable} = " + json.dumps(news, ensure_ascii=True) + ";\n", encoding="utf-8")
    os.replace(temp, path)


class NewsWriter:
    """Rewrites news.js, and geo.js next to it, a few seconds after any web feed delivers new
    data or stops. `path` is the news.js to write (in the web folder)."""

    def __init__(self, cfg: dict, service: FeedService, path: str | Path) -> None:
        self.cfg, self.service, self.path = cfg, service, Path(path)
        self.names = web_feeds(cfg)
        self.geo_names = geo_feeds(cfg)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        service.on_update(self._on_update)
        service.on_failure(lambda name, _reason: self._on_update(name))   # the page greys it out

    def _on_update(self, name: str) -> None:
        if name not in self.names and name not in self.geo_names:
            return
        with self._lock:                                   # not restarted, so a steady stream can't postpone it
            if self._timer is None:
                self._timer = threading.Timer(WRITE_DELAY_MS / 1000, self._timed_write)
                self._timer.daemon = True
                self._timer.start()

    def _timed_write(self) -> None:
        with self._lock:
            self._timer = None
        self.write()

    def write(self) -> None:
        try:
            with_icon = {key for key in self.names if icons.cached_icon(key)}   # icons found since then come along
            write_news(self.path, news_object(self.cfg, self.service, self.names, with_icon))
            write_images(self.service, self.geo_names, self.path.parent / IMAGE_FOLDER)   # before geo.js points at them
            write_news(self.path.parent / "geo.js", geo_object(self.cfg, self.service, self.geo_names), "GEO")
        except OSError as ex:
            print(f"web news: could not write {self.path}: {ex}")
