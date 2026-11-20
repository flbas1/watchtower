"""The small web server for the page: serves the web folder (index.html, images, and the
news.js / geo.js / icons / layers that watchtower writes), and one lookup:

  /adsb/callsign/<CALLSIGN> and /adsb/registration/<TAIL>: the aircraft adsb.fi sees under
  that callsign or tail number now, as {"items": [...]} in geo.js's item form. The page's
  "Track" boxes ask for these (adsb.fi doesn't let web pages ask it themselves). Nothing is
  saved; an answer is reused for LOOKUP_SECONDS."""

import json
import re
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from .feeds import AdsbFiProcessor

LOOKUP = re.compile(r"^/adsb/(callsign|registration)/([A-Za-z0-9-]{2,10})$")
LOOKUP_SECONDS = 30
_lookups: dict[str, tuple[float, int, dict]] = {}       # adsb.fi path -> (when, status, answer)
_lookup_lock = threading.Lock()
_client: httpx.Client | None = None


def adsb_lookup(path: str, client: httpx.Client | None = None) -> tuple[int, dict]:
    """(status, answer) for /adsb/<kind>/<ident>. Kept LOOKUP_SECONDS, so several browsers and
    maps asking for the same plane cost adsb.fi one request."""
    global _client
    kind, ident = LOOKUP.match(path).groups()
    key = f"/{kind}/{ident.upper()}"
    with _lookup_lock:
        cached = _lookups.get(key)
        if cached and time.monotonic() - cached[0] < LOOKUP_SECONDS:
            return cached[1], cached[2]
        if client is None:
            _client = _client or httpx.Client(timeout=20, headers={"User-Agent": "watchtower"})
            client = _client
    reader = AdsbFiProcessor({"name": "Tracked"})
    try:
        planes = reader._get(client, key)                   # spaced out with every flight feed's requests
        items = [reader._row(p) for p in planes if p.get("lat") is not None and p.get("lon") is not None]
        status, answer = 200, {"items": json.loads(json.dumps(items, default=str))}
    except (httpx.HTTPError, ValueError) as ex:
        status, answer = 502, {"error": f"{type(ex).__name__}: {ex}"}
    with _lookup_lock:
        _lookups[key] = (time.monotonic(), status, answer)
    return status, answer


class _Quiet(SimpleHTTPRequestHandler):
    """The standard file handler, without a log line for every request (the page asks for
    news.js and geo.js every minute), plus the aircraft lookup."""

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if not LOOKUP.match(path):
            super().do_GET()
            return
        status, answer = adsb_lookup(path)
        body = json.dumps(answer).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        if args and str(args[1])[:1] in "45":                 # only errors: 4xx / 5xx
            super().log_message(format, *args)

    def end_headers(self) -> None:
        if self.path.split("?")[0].endswith((".js", ".html")):
            self.send_header("Cache-Control", "no-cache")      # always the newest news, page and code
        super().end_headers()


def start_server(folder: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Serve `folder` on host:port on a background thread; stop it with .shutdown()."""
    server = ThreadingHTTPServer((host, port), partial(_Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, name="web-server", daemon=True).start()
    return server
