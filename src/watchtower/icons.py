"""Feed icons: the feed's own logo if it has one, otherwise the website's favicon.

Found once per feed and cached as a PNG in the web folder's `icons` folder
(`web/icons/<feed key>.png`), where the page reads it, so later runs make no request.
Delete a file to make the app look again. A feed with no usable icon gets an empty `icons/<feed key>.none`
marker instead, and isn't looked for again until it's RETRY_DAYS old.

Where the icon comes from, in order:
  1. the feed's own logo: RSS 2.0 / 1.0 `<image><url>`, Atom `<icon>` then `<logo>`;
  2. the website's icon, from its home page's `<link rel="apple-touch-icon" | "icon">`;
  3. the website's `/favicon.ico`.
The website is the one the feed names (its `<link>`), or else the feed URL's own site.
SVG icons are skipped (Pillow can't read them); the next candidate is tried.
"""

import io
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

from .feeds import AUTH_TYPES, element_link, local_name

MAX_SIZE = (192, 64)       # stored at most this big (wide logos keep their shape); shown smaller
RETRY_DAYS = 7             # a feed with no icon is looked for again after this long
ICON_FOLDER = Path("web/icons")   # set at startup to <web folder>/icons

# what can go wrong finding an icon on someone else's website; none of it is worth more than
# "no icon" (anything else is a bug in this file and is allowed to surface)
_EXPECTED = (httpx.HTTPError, OSError, ValueError, KeyError, ET.ParseError, Image.DecompressionBombError)


def icon_dir() -> Path:
    ICON_FOLDER.mkdir(parents=True, exist_ok=True)
    return ICON_FOLDER


def cached_icon(key: str) -> Path | None:
    """The saved icon for this feed key, or None if there isn't one (yet)."""
    path = icon_dir() / f"{key}.png"
    return path if path.exists() else None


def _recently_failed(key: str) -> bool:
    marker = icon_dir() / f"{key}.none"
    return marker.exists() and time.time() - marker.stat().st_mtime < RETRY_DAYS * 86400


def ensure_icon(client: httpx.Client, key: str, feed: dict,
                response: httpx.Response | None = None) -> Path | None:
    """Find and save this feed's icon unless it's already saved (or was looked for in vain
    lately). Blocks (network): call it from a worker thread. `response`: the feed's download,
    if the caller already has it. Returns None when there's no icon."""
    if (path := cached_icon(key)) is not None:
        return path
    if not feed.get("url") or _recently_failed(key):
        return None                                  # calculated feeds (the sun) have no website
    try:
        if response is None:
            auth = AUTH_TYPES[feed.get("authtype") or "none"](feed).request_args()
            response = client.get(feed["url"], **auth)
        logo, site = _from_feed(response)
        site = site or _site_root(str(response.url))
        for candidate in [logo, *_from_home_page(client, site), urljoin(site, "/favicon.ico")]:
            if candidate and (image := _download_image(client, candidate)) is not None:
                path = icon_dir() / f"{key}.png"
                temp = path.with_name(path.name + ".tmp")
                image.save(temp, "PNG")
                temp.replace(path)
                return path
    except _EXPECTED:
        pass                                         # no icon is fine: the badge shows the name alone
    (icon_dir() / f"{key}.none").touch()             # remember the miss, so it isn't retried every start
    return None


def _from_feed(response: httpx.Response) -> tuple[str | None, str | None]:
    """(the feed's own logo URL, the website it names) from a feed download; Nones if it
    isn't a feed we can read."""
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return None, None
    base = str(response.url)
    channel = next((e for e in root.iter() if local_name(e.tag) in ("channel", "feed")), root)
    site = urljoin(base, element_link(channel)) if element_link(channel) else None
    for element in root.iter():
        if local_name(element.tag) == "image":      # RSS: <image><url>…</url></image>
            for child in element:
                if local_name(child.tag) == "url" and child.text and child.text.strip():
                    return urljoin(base, child.text.strip()), site
    for name in ("icon", "logo"):                    # Atom: <icon> is square, <logo> is wide
        for child in channel:
            if local_name(child.tag) == name and child.text and child.text.strip():
                return urljoin(base, child.text.strip()), site
    return None, site


def _site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


class _IconLinks(HTMLParser):
    """Collects (rank, href) for every <link rel="…icon…"> on a page."""
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag != "link":
            return
        attrs = dict(attrs)
        rel, href = (attrs.get("rel") or "").lower(), attrs.get("href")
        if not href or ("icon" not in rel.split() and "apple-touch-icon" not in rel):
            return
        rank = 0 if "apple-touch-icon" in rel else 1  # apple-touch icons are big (180 px): best for scaling down
        self.found.append((rank, href))


def _from_home_page(client: httpx.Client, site: str) -> list[str]:
    """Icon URLs the website's home page lists, best first."""
    try:
        page = client.get(site)
        if page.status_code >= 400 or "html" not in page.headers.get("content-type", ""):
            return []
        parser = _IconLinks()
        parser.feed(page.text)
        return [urljoin(str(page.url), href) for _, href in sorted(parser.found, key=lambda f: f[0])]
    except _EXPECTED:
        return []


def _download_image(client: httpx.Client, url: str) -> Image.Image | None:
    """The picture at `url`, shrunk to MAX_SIZE, or None if it isn't a picture Pillow reads."""
    if url.lower().split("?")[0].endswith(".svg"):
        return None
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return None
        image = Image.open(io.BytesIO(response.content))   # .ico files open at their largest size
        image.load()
    except _EXPECTED:
        return None
    if min(image.size) < 8:                          # a 1-pixel tracking image isn't a logo
        return None
    image = image.convert("RGBA")
    image.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
    return image
