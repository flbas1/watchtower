"""The parts watchtower wrote fresh: the feed scheduler, the config loader, the web server and
--once."""
import threading
import time
import urllib.request

import pandas as pd

from watchtower import config
from watchtower.feed_service import FeedService
from watchtower.server import start_server


class FakeFeed:
    def __init__(self, name, minutes=60, fail=False):
        self.name, self.update_minutes, self.fail, self.calls = name, minutes, fail, 0
    def fetch(self, client):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        return pd.DataFrame({"title": [f"{self.name} story", "[Sponsor] ad"]})


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not condition():
        time.sleep(0.02)
    return condition()


def test_service_fetches_every_feed_tells_listeners_and_skips_keywords():
    service = FeedService(["sponsor"])
    good, bad = FakeFeed("good"), FakeFeed("bad", fail=True)
    service.add(good); service.add(bad)
    updated, failed = [], []
    service.on_update(updated.append)
    service.on_failure(lambda name, reason: failed.append((name, reason)))
    service.start()
    try:
        assert wait_for(lambda: updated and failed)
        assert updated == ["good"] and failed[0][0] == "bad"
        assert failed[0][1].startswith("ConnectionError: down (trying again at ")
        assert list(service.latest("good")["title"]) == ["good story"]                # the sponsor story is dropped
        assert list(service.failed()) == ["bad"] and service.fetched_at("good") is not None
        time.sleep(0.6)
        assert bad.calls == 1                                                        # not again before the retry time
    finally:
        service.stop()


class FlakyFeed(FakeFeed):
    """Fails `failures` times, then works."""
    def __init__(self, name, failures):
        super().__init__(name)
        self.failures, self.times = failures, []
    def fetch(self, client):
        self.times.append(time.monotonic())
        if len(self.times) <= self.failures:
            raise ConnectionError("down")
        return pd.DataFrame({"title": ["back"]})


def test_failed_feed_retries_longer_each_time_then_recovers():
    service = FeedService(retry={"first_minutes": 0.6 / 60, "max_minutes": 10})     # 0.6 s, then 1.2 s (the scheduler naps 0.5 s at least)
    flaky = FlakyFeed("flaky", failures=2)
    service.add(flaky)
    updated = []
    service.on_update(updated.append)
    service.start()
    try:
        assert wait_for(lambda: updated == ["flaky"], seconds=8)                       # back, and the page is told
        gaps = [b - a for a, b in zip(flaky.times, flaky.times[1:])]
        # each wait is its retry time (0.6 s, then doubled: 1.2 s) plus up to one 0.5 s nap
        assert 0.55 < gaps[0] < 1.3 and 1.15 < gaps[1] < 1.9
        assert service.failed() == {} and list(service.latest("flaky")["title"]) == ["back"]
    finally:
        service.stop()


def test_paced_site_requests_are_spaced():
    import httpx
    from watchtower.feed_service import SharedDownloads
    asked = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (asked.append((r.url.host, time.monotonic())), httpx.Response(200))[1]))
    shared = SharedDownloads(client, pace={"www.reddit.com": 0.3})
    threads = [threading.Thread(target=shared.get, args=(f"https://www.reddit.com/r/{n}/.rss",)) for n in range(3)]
    threads.append(threading.Thread(target=shared.get, args=("https://other.example/feed",)))
    for t in threads: t.start()
    for t in threads: t.join()
    reddit = sorted(when for host, when in asked if host == "www.reddit.com")
    assert len(reddit) == 3 and all(b - a >= 0.29 for a, b in zip(reddit, reddit[1:]))   # one at a time, 0.3 s apart
    assert any(host == "other.example" for host, _ in asked)                              # other sites aren't held up


def test_service_refetches_when_due():
    service = FeedService()
    quick = FakeFeed("quick", minutes=0.5 / 60)                                      # every half second
    service.add(quick)
    service.start()
    try:
        assert wait_for(lambda: quick.calls >= 3, seconds=4)
    finally:
        service.stop()


def test_config_defaults_and_overrides(tmp_path):
    (tmp_path / "config.yml").write_text(
        "server: {port: 9999}\nfeed_catalog:\n  bbc: null\n  mine: {format: rss, url: 'https://x/rss'}\n", encoding="utf-8")
    cfg = config.load_config(tmp_path / "config.yml")
    assert cfg["server"] == {"host": "127.0.0.1", "port": 9999}                     # given + default
    assert "bbc" not in cfg["feed_catalog"] and "earthquakes" in cfg["feed_catalog"]   # null turns a default off
    assert cfg["feed_catalog"]["mine"]["name"] == "mine" and cfg["feed_catalog"]["mine"]["update_minutes"] == 60
    assert cfg["web"]["folder"] == config.PROJECT / "web"


def test_server_serves_the_web_folder_only(tmp_path):
    web = tmp_path / "web"; web.mkdir()
    (web / "index.html").write_text("<p>page</p>", encoding="utf-8")
    (tmp_path / "config.yml").write_text("secret", encoding="utf-8")
    server = start_server(web, "127.0.0.1", 0)                                       # 0: any free port
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html") as r:
            assert r.read() == b"<p>page</p>" and r.headers["Cache-Control"] == "no-cache"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../config.yml")
            assert False, "outside the web folder must not be served"
        except urllib.error.HTTPError as ex:
            assert ex.code == 404
    finally:
        server.shutdown()
