"""Watchtower: open-source intelligence feeds (news, weather, flights, satellites, outages...)
fetched in the background and shown on a web page of marquees and maps. Headless: runs on a
server or in a container, and you open the page in a browser.

    uv run watchtower                  # until Ctrl+C (or SIGTERM from systemd)
    uv run watchtower --once           # fetch everything once, write the files, exit
    uv run watchtower --config FILE    # another config.yml
"""

import argparse
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from . import icons
from .config import load_config
from .feed_service import USER_AGENT, FeedService
from .feeds import Feed
from .server import start_server
from .web_news import NewsWriter, geo_feeds, web_feeds

ONCE_TIMEOUT_SECONDS = 300      # --once: give up on feeds that haven't answered by then


def build_service(cfg: dict) -> tuple[FeedService, set[str]]:
    """A feed service with every feed the page can show (marquees and maps)."""
    skip = cfg.get("skip_keywords") or []
    service = FeedService([skip] if isinstance(skip, str) else list(skip),   # one word written without [ ] still works
                          pace=cfg.get("pace"), retry=cfg.get("retry"))
    names = set()
    for name in sorted(web_feeds(cfg) | geo_feeds(cfg)):
        try:
            service.add(Feed(name, cfg["feed_catalog"][name]))
            names.add(name)
        except KeyError as ex:                             # an unknown format: or authtype: in the YAML
            print(f"feed '{name}': unknown {ex} (check its format / authtype), skipped")
    return service, names


def find_missing_icons(cfg: dict, names: set[str]) -> threading.Thread | None:
    """Feeds without a saved icon look for one once, in the background; the next news.js
    names the icons found."""
    missing = sorted(n for n in web_feeds(cfg) & names if icons.cached_icon(n) is None)
    if not missing:
        return None

    def find() -> None:
        with (httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(8.0, connect=4.0),
                           follow_redirects=True) as client,
              ThreadPoolExecutor(8, thread_name_prefix="icons") as pool):
            list(pool.map(lambda k: icons.ensure_icon(client, k, dict(cfg["feed_catalog"][k])), missing))
    thread = threading.Thread(target=find, name="icons", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchtower", description="OSINT feeds for a web page of marquees and maps.")
    parser.add_argument("--config", type=Path, help="config file (default: config.yml in the project folder)")
    parser.add_argument("--once", action="store_true", help="fetch every feed once, write the page's files, and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    web = cfg["web"]["folder"]
    icons.ICON_FOLDER = web / "icons"
    service, names = build_service(cfg)
    writer = NewsWriter(cfg, service, web / "news.js")
    finding = find_missing_icons(cfg, names)
    service.start()
    print(f"watchtower: {len(names)} feeds, writing to {web}")

    if args.once:
        deadline = time.monotonic() + ONCE_TIMEOUT_SECONDS
        while time.monotonic() < deadline and any(service.fetched_at(n) is None and n not in service.failed() for n in names):
            time.sleep(1)
        if finding:
            finding.join(timeout=max(0, deadline - time.monotonic()))
        writer.write()
        waiting = [n for n in names if service.fetched_at(n) is None and n not in service.failed()]
        print(f"watchtower: done; {len(service.failed())} feeds failed, {len(waiting)} didn't answer in time")
        service.stop()
        return 0

    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    server = start_server(web, host, port)
    print(f"watchtower: the page is at http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/ (Ctrl+C to stop)")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(1):                                # wait() with a timeout lets Ctrl+C through on Windows
        pass
    print("watchtower: stopping")
    server.shutdown()
    service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
