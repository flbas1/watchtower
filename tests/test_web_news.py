import io
import json

import pandas as pd
from PIL import Image

from watchtower import icons
from watchtower.web_news import NewsWriter, geo_feeds, geo_object, news_object, web_feeds, write_news

CFG = {"feed_catalog": {
    "zeta": {"name": "Zeta News", "format": "rss"},
    "alpha": {"name": "alpha wire", "format": "rss"},
    "quakes": {"name": "Quakes", "format": "geojson"},
    "empty": {"name": "Empty", "format": "rss"},
}}


class FakeService:
    def __init__(self, data, failed=None):
        self.data, self._failed = data, failed or {}
    def latest(self, name):
        return self.data.get(name)
    def failed(self):
        return dict(self._failed)


def test_web_feeds_are_the_rss_feeds():
    assert web_feeds(CFG) == {"zeta", "alpha", "empty"}


def test_alerts_and_storm_reports_are_in_both_files():
    cfg = {"feed_catalog": {"news": {"format": "rss"}, "alerts": {"format": "nws_alerts"},
                            "reports": {"format": "iem_storm_reports"}, "quakes": {"format": "usgs_quakes"}}}
    assert web_feeds(cfg) == {"news", "alerts", "reports"}               # marquees
    assert geo_feeds(cfg) == {"alerts", "reports", "quakes"}             # maps


def test_news_object_every_feed_newest_first(tmp_path):
    service = FakeService({
        "zeta": pd.DataFrame({"title": ["Old", "New", None],
                              "url": ["https://z/1", "https://z/2", "https://z/3"],
                              "time": [pd.Timestamp("2026-11-01", tz="UTC"), pd.Timestamp("2026-11-02", tz="UTC"), pd.NaT]}),
        "alpha": pd.DataFrame({"title": ["Undated"], "url": [None], "time": [pd.NaT]}),
    }, failed={"empty": "ConnectError: reset"})
    news = news_object(CFG, service, web_feeds(CFG), with_icon={"zeta"})
    assert list(news["feeds"]) == ["alpha", "empty", "zeta"]          # by English name, any case
    assert [s["title"] for s in news["feeds"]["zeta"]["stories"]] == ["New", "Old"]   # untitled dropped
    assert news["feeds"]["alpha"]["stories"] == [{"title": "Undated", "url": None, "time": None}]
    assert news["feeds"]["empty"] == {"name": "Empty", "icon": None, "failed": "ConnectError: reset", "stories": []}
    assert news["feeds"]["zeta"]["icon"] == "icons/zeta.png"

    path = tmp_path / "news.js"
    write_news(path, news)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("window.NEWS = ") and json.loads(text[len("window.NEWS = "):-2]) == news
    assert not (tmp_path / "news.js.tmp").exists()


def test_geo_object_points_and_shapes():
    cfg = {"location": {"lat": 25.76, "lon": -80.19}, "feed_catalog": {
        "quakes": {"name": "USGS", "format": "usgs_quakes"},
        "storms": {"name": "NHC storms", "format": "nhc_storms"},
        "news": {"name": "News", "format": "rss"},
    }}
    assert geo_feeds(cfg) == {"quakes", "storms"}
    service = FakeService({
        "quakes": pd.DataFrame([{"title": "M 5.1", "url": "https://q/1", "source": "USGS", "time": pd.Timestamp("2026-11-18", tz="UTC"),
                                 "lat": 10.5, "lon": -20.0, "magnitude": 5.1}]),
        "storms": pd.DataFrame([{"title": "Pena", "url": None, "source": "NHC", "time": None, "lat": 15.0, "lon": -120.0,
                                 "geometry": [("line", [(14.0, -118.0), (15.0, -120.0)])]}]),
    })
    geo = geo_object(cfg, service, geo_feeds(cfg))
    assert geo["location"] == {"lat": 25.76, "lon": -80.19}
    assert list(geo["feeds"]) == ["storms", "quakes"]                 # by name: "NHC storms" before "USGS"
    quake = geo["feeds"]["quakes"]["items"][0]
    assert (quake["lat"], quake["lon"], quake["magnitude"], quake["geometry"]) == (10.5, -20.0, 5.1, [])
    storm = geo["feeds"]["storms"]["items"][0]
    assert storm["geometry"] == [["line", [[14.0, -118.0], [15.0, -120.0]]]] and storm["magnitude"] is None
    assert geo["feeds"]["storms"]["format"] == "nhc_storms"


class ListeningService(FakeService):
    """FakeService plus the listener hooks NewsWriter registers."""
    def on_update(self, listener): self.updated = listener
    def on_failure(self, listener): self.failed_listener = listener


def test_writer_writes_news_geo_and_pictures(web_folder):
    Image.new("RGBA", (16, 16)).save(icons.icon_dir() / "zeta.png")            # a cached icon in the page's folder
    png = io.BytesIO(); Image.new("RGBA", (2, 2)).save(png, "PNG")
    cfg = {"location": {"lat": 1, "lon": 2}, "feed_catalog": dict(CFG["feed_catalog"],
           radar={"name": "Radar", "format": "iem_radar"})}
    service = ListeningService({
        "zeta": pd.DataFrame({"title": ["New"], "url": ["https://z/2"], "time": [pd.Timestamp("2026-11-02", tz="UTC")]}),
        "radar": pd.DataFrame([{"title": "Radar", "image": png.getvalue(), "bounds": [24, -88, 31, -79], "id": "v1",
                                "lat": None, "lon": None, "time": None}]),
    })
    writer = NewsWriter(cfg, service, web_folder / "news.js")
    writer.write()
    news = json.loads((web_folder / "news.js").read_text(encoding="utf-8")[len("window.NEWS = "):-2])
    geo = json.loads((web_folder / "geo.js").read_text(encoding="utf-8")[len("window.GEO = "):-2])
    assert news["feeds"]["zeta"]["icon"] == "icons/zeta.png" and news["feeds"]["alpha"]["icon"] is None
    assert geo["feeds"]["radar"]["items"][0]["image"] == "layers/radar.png?v=v1"
    assert (web_folder / "layers" / "radar.png").exists()


def test_writer_waits_a_moment_then_writes_once(web_folder, monkeypatch):
    monkeypatch.setattr("watchtower.web_news.WRITE_DELAY_MS", 50)
    service = ListeningService({})
    writer = NewsWriter({"location": {}, "feed_catalog": CFG["feed_catalog"]}, service, web_folder / "news.js")
    writes = []
    writer.write = lambda: writes.append(1)
    for _ in range(5):
        service.updated("zeta")                                                # five updates close together
    service.updated("quakes")                                                  # geo feeds count too
    import time; time.sleep(0.3)
    assert writes == [1]
