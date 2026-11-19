"""The small web server for the page: serves the web folder (index.html, images, and the
news.js / geo.js / icons / layers that watchtower writes), and nothing else."""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Quiet(SimpleHTTPRequestHandler):
    """The standard file handler, without a log line for every request (the page asks for
    news.js and geo.js every minute)."""

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
