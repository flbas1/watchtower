import email.utils
import hashlib
import html
import io
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from abc import ABCMeta, abstractmethod
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

import httpx
import numpy as np
import pandas as pd
from PIL import Image

from .sun import subsolar_point

COLUMNS = ["title", "url", "source", "time", "lat", "lon"]

class FeedProcessor(metaclass=ABCMeta):
    def __init__(self, settings:dict)-> None:
        self.settings = settings

    @abstractmethod
    def process(self, client: httpx.Client, response: httpx.Response | None) -> pd.DataFrame:
        pass

class AuthType(metaclass=ABCMeta):
    def __init__(self, settings:dict)-> None:
        self.settings = settings

    @abstractmethod
    def request_args(self) -> dict:
        pass

class RssProcessor(FeedProcessor):
    """News feeds in any of the three common formats:
      RSS 2.0  <item> with <title>, <link>text</link>, <pubDate>
      RSS 1.0  the same, but every tag carries a namespace ("{http://purl.org/rss/1.0/}item"),
               and the date is <dc:date>
      Atom     <entry> with <title>, <link href="..."/>, <updated> or <published>
    Tags are matched by their local name (the part after any "{namespace}"), so one loop
    reads all three."""

    def process(self, client, response) -> pd.DataFrame:
        root = ET.fromstring(response.content)
        # relative item links ("/a/b") are relative to the website the feed names, not the feed's own address
        channel = next((e for e in root.iter() if local_name(e.tag) in ("channel", "feed")), root)
        site = urljoin(str(response.url), element_link(channel) or "")
        rows = []
        for item in root.iter():
            if local_name(item.tag) not in ("item", "entry"):
                continue
            rows.append({
                "title": _child_text(item, "title"),
                "url": urljoin(site, element_link(item) or "") or None,   # "/a/b" -> "https://site/a/b"
                "source": self.settings["name"],
                "time": _parse_date(_child_text(item, "pubDate", "date", "published", "updated")),
                "lat": None,
                "lon": None,
            })
        return pd.DataFrame(rows, columns=COLUMNS)


def local_name(tag: str) -> str:
    """'{http://www.w3.org/2005/Atom}entry' -> 'entry'; plain tags come back unchanged."""
    return tag.rsplit("}", 1)[-1]


def _child_text(item: ET.Element, *names: str) -> str | None:
    """Text of the first direct child whose local name is one of `names` (tried in order)."""
    for name in names:
        for child in item:
            if local_name(child.tag) == name and child.text and child.text.strip():
                return child.text.strip()
    return None


def element_link(item: ET.Element) -> str | None:
    """RSS puts the link in the text; Atom puts it in href=, preferring rel="alternate"."""
    links = [child for child in item if local_name(child.tag) == "link"]
    for link in links:
        if link.text and link.text.strip():
            return link.text.strip()                      # RSS 2.0 / 1.0
    for link in links:
        if link.get("href") and link.get("rel", "alternate") == "alternate":
            return link.get("href")                       # Atom: the page itself
    return links[0].get("href") if links else None


def _parse_date(text: str | None) -> datetime | None:
    """RSS dates ('Fri, 06 Nov 2026 20:15:00 GMT') or ISO dates ('2026-11-11T17:04:56Z').
    A date that can't be read gives None for that item instead of failing the whole feed.
    A date without a timezone is taken as UTC."""
    if not text:
        return None
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


class SunProcessor(FeedProcessor):
    """Where the sun is straight overhead right now. Nothing to download (no url): calculated.
    Each update moves the sun, which is the regular reason for the map to redraw."""

    def process(self, client, response) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        lat, lon = subsolar_point(now)
        return pd.DataFrame([{"title": "Sun overhead", "url": None, "source": "Sun",
                              "time": now, "lat": lat, "lon": lon}], columns=COLUMNS)


# Extra columns any feed may add (all optional; views and alerts use them when present):
#   id         a stable id for the event, so an alert fires once per event
#   magnitude  a number alerts can compare with min_magnitude (earthquakes)
#   details    one line of extra facts for the alert window's story panel
EVENT_COLUMNS = COLUMNS + ["id", "magnitude", "details"]


class UsgsQuakeProcessor(FeedProcessor):
    """Earthquakes from a USGS GeoJSON summary feed, e.g.
    https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson
    One row per quake: "M 5.1 - 80 km W of Panguna, Papua New Guinea" at its epicentre."""

    def process(self, client, response) -> pd.DataFrame:
        rows = []
        for quake in response.json().get("features", []):
            p = quake.get("properties") or {}
            lon, lat, depth = (quake.get("geometry") or {}).get("coordinates", [None, None, None])[:3]
            facts = [f"Depth {depth:.0f} km" if depth is not None else None,
                     "TSUNAMI warning issued" if p.get("tsunami") else None,
                     f"USGS PAGER alert: {p['alert']}" if p.get("alert") else None]
            rows.append({
                "title": p.get("title") or f"M {p.get('mag')} - {p.get('place')}",
                "url": p.get("url"),
                "source": self.settings["name"],
                "time": pd.to_datetime(p.get("time"), unit="ms", utc=True) if p.get("time") else None,
                "lat": lat, "lon": lon,
                "id": quake.get("id"),
                "magnitude": p.get("mag"),
                "details": " · ".join(f for f in facts if f),
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- hurricanes: three processors reading the National Hurricane Center ------------------
#
# All three read NHC's CurrentStorms.json (the list of active storms; the generic Feed
# downloads it and handles the ETag), then each picks its own part, downloading the
# storm's KMZ files where needed:
#   nhc_storms -> where each storm is now           (a point: lat / lon)
#   nhc_track  -> where each storm has been         (geometry: one line)
#   nhc_cone   -> where it may go: cone + forecast  (geometry: a polygon and points)
#
# Extra columns beyond the contract's six:
#   id       NHC's storm id (e.g. "cp042026"), the same in all three, to match them up
#   geometry list of (kind, [(lat, lon), ...]) with kind "line", "polygon" or "point"

NHC_COLUMNS = COLUMNS + ["id", "geometry"]


class _NhcProcessor(FeedProcessor):
    """Shared by the three NHC processors: one row per active storm.
    The leading underscore marks it as internal; PROCESSORS only lists the three below."""

    KINDS = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression",
             "STS": "Subtropical Storm", "SD": "Subtropical Depression",
             "PTC": "Potential Tropical Cyclone", "PC": "Post-tropical Cyclone"}

    def process(self, client, response) -> pd.DataFrame:
        rows = [self.row(client, storm) for storm in response.json().get("activeStorms", [])]
        return pd.DataFrame(rows, columns=NHC_COLUMNS)     # columns= keeps the shape even with no storms

    @abstractmethod
    def row(self, client: httpx.Client, storm: dict) -> dict:
        """One item (a dict with NHC_COLUMNS keys) for one storm."""

    def base_row(self, storm: dict, what: str) -> dict:
        """The columns every hurricane processor fills the same way."""
        kind = self.KINDS.get(storm.get("classification"), storm.get("classification"))
        return {
            "title": f"{kind} {storm['name']}{what}",
            "url": (storm.get("publicAdvisory") or {}).get("url"),
            "source": "NHC",
            "time": pd.to_datetime(storm.get("lastUpdate"), utc=True),
            "lat": None, "lon": None,
            "id": storm.get("id"),
            "geometry": [],
        }


class NhcStormsProcessor(_NhcProcessor):
    """Where each active storm is now: a point, shown as a marker (and a marquee link)."""

    def row(self, client, storm):
        item = self.base_row(storm, f", {storm['intensity']} kt")
        item["lat"] = storm.get("latitudeNumeric")
        item["lon"] = storm.get("longitudeNumeric")        # west is negative, same as our maps
        return item


class NhcTrackProcessor(_NhcProcessor):
    """Where each storm has been: its observed positions joined into one line."""

    def row(self, client, storm):
        item = self.base_row(storm, " past track")
        shapes = kmz_shapes(client, (storm.get("bestTrackGIS") or {}).get("kmzFile"))
        points = [pts[0] for kind, pts in shapes if kind == "point"]   # one <Point> per observation, oldest first
        if points:
            item["geometry"] = [("line", points + [(storm["latitudeNumeric"], storm["longitudeNumeric"])])]
        return item


class NhcConeProcessor(_NhcProcessor):
    """Where each storm may go: the forecast cone (a polygon) and the forecast points."""

    def row(self, client, storm):
        item = self.base_row(storm, " forecast")
        cone = kmz_shapes(client, (storm.get("trackCone") or {}).get("kmzFile"))
        forecast = kmz_shapes(client, (storm.get("forecastTrack") or {}).get("kmzFile"))
        item["geometry"] = ([shape for shape in cone if shape[0] == "polygon"]
                            + [shape for shape in forecast if shape[0] == "point"])
        return item


def kmz_shapes(client: httpx.Client, url: str | None) -> list[tuple[str, list[tuple[float, float]]]]:
    """Download a KMZ and return its shapes as [(kind, [(lat, lon), ...]), ...] in file order.

    A KMZ is a zip file holding one KML (XML) file. kind is "point", "line" or "polygon"
    (outer edge only). Empty list if there is no URL (NHC hasn't published one yet).
    """
    if not url:
        return []
    response = client.get(url)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as kmz:
        kml_name = next(n for n in kmz.namelist() if n.lower().endswith(".kml"))
        root = ET.fromstring(kmz.read(kml_name))

    kinds = {"Point": "point", "LineString": "line", "Polygon": "polygon"}
    shapes = []
    for element in root.iter():
        kind = kinds.get(local_name(element.tag))           # KML tags carry a namespace: "{...}Point"
        if kind is None:
            continue
        coords = next((c.text for c in element.iter() if c.tag.endswith("coordinates")), None)
        if not coords:
            continue
        points = []
        for triple in coords.split():                       # "lon,lat,alt lon,lat,alt ..."
            lon, lat = (float(v) for v in triple.split(",")[:2])   # KML order is lon,lat
            points.append((lat, lon))
        shapes.append((kind, points))
    return shapes


# --- weather service alerts and storm reports ----------------------------------------------
#
# Both add a `geometry` column (the same shapes as the hurricanes) and the event columns.

WEATHER_COLUMNS = EVENT_COLUMNS + ["geometry"]
MAX_RING_POINTS = 150          # zone outlines are detailed (hundreds of points); this many is plenty on a map


def _rings(geometry: dict | None) -> list[list[tuple[float, float]]]:
    """The outer edges of a GeoJSON Polygon / MultiPolygon as [(lat, lon), ...] lists,
    each thinned to at most MAX_RING_POINTS points (holes are left out)."""
    if not geometry:
        return []
    polygons = {"Polygon": [geometry.get("coordinates") or []],
                "MultiPolygon": geometry.get("coordinates") or []}.get(geometry.get("type"), [])
    rings = []
    for polygon in polygons:
        if not polygon:
            continue
        outer = polygon[0]                                    # GeoJSON order is [lon, lat]
        step = max(1, -(-len(outer) // MAX_RING_POINTS))      # ceiling division
        rings.append([(lat, lon) for lon, lat, *_ in outer[::step]])
    return rings


def _middle(rings: list[list[tuple[float, float]]]) -> tuple[float | None, float | None]:
    """A point inside-ish the shapes (the average of their points), where the marker goes."""
    points = [p for ring in rings for p in ring]
    if not points:
        return None, None
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


class NwsAlertsProcessor(FeedProcessor):
    """Active National Weather Service alerts (warnings, watches, advisories, statements),
    e.g. url https://api.weather.gov/alerts/active?area=FL
    Optional setting `sender`: keep only one office's alerts, e.g. "NWS Miami FL".

    One row per alert, titled "Flash Flood Warning: Broward, FL", with its area as polygons.
    Warnings come with their own polygon; watches and advisories only name forecast zones,
    so the zones' outlines are downloaded (once per zone while the app runs)."""

    HAZARD_PAGE = "https://forecast.weather.gov/showsigwx.php"   # the readable text of an area's alerts

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self._zones: dict[str, list] = {}         # zone URL -> its rings, kept between updates
        self._county_zone: dict[str, str | None] = {}   # county code -> a forecast zone in it

    def process(self, client, response) -> pd.DataFrame:
        sender = self.settings.get("sender")
        rows = []
        for alert in response.json().get("features", []):
            p = alert.get("properties") or {}
            if sender and p.get("senderName") != sender:
                continue
            rings = _rings(alert.get("geometry"))
            if not rings:
                for zone in p.get("affectedZones") or []:
                    rings += self._zone(client, zone)
            lat, lon = _middle(rings)
            area = p.get("areaDesc") or ""
            ends = p.get("ends") or p.get("expires")
            facts = [p.get("severity"), p.get("urgency"),
                     f"until {pd.to_datetime(ends).strftime('%a %d %b %H:%M')}" if ends else None]
            rows.append({
                "title": f"{p.get('event')}: {area if len(area) <= 80 else area[:77] + '...'}",
                "url": self._hazard_link(client, p, lat, lon),
                "source": self.settings["name"],
                "time": pd.to_datetime(p.get("sent"), utc=True) if p.get("sent") else None,
                "lat": lat, "lon": lon,
                "id": p.get("id"),
                "magnitude": None,
                "details": " · ".join(f for f in facts if f),
                "geometry": [("polygon", ring) for ring in rings],
            })
        return pd.DataFrame(rows, columns=WEATHER_COLUMNS)

    def _hazard_link(self, client, p: dict, lat: float | None, lon: float | None) -> str | None:
        """forecast.weather.gov's hazard page for the alert's area, opened on this alert:
        its full text, readable. The page needs a forecast zone and a county: watches and
        advisories name zones (FLZ172); warnings name only counties (FLC086), so a zone in
        that county is looked up once at the alert's spot. Without a zone: the API page."""
        ugc = (p.get("geocode") or {}).get("UGC") or []
        same = (p.get("geocode") or {}).get("SAME") or []
        zone = next((u for u in ugc if len(u) > 2 and u[2] == "Z"), None)
        county = (ugc[0][:2] + "C" + same[0][-3:]) if ugc and same else next((u for u in ugc if len(u) > 2 and u[2] == "C"), None)
        if zone is None and county and lat is not None:
            zone = self._zone_in_county(client, county, lat, lon)
        if zone is None:
            return p.get("@id")
        query = {"warnzone": zone, "warncounty": county, "product1": p.get("event")}
        return str(httpx.URL(self.HAZARD_PAGE, params={k: v for k, v in query.items() if v}))

    def _zone_in_county(self, client, county: str, lat: float, lon: float) -> str | None:
        if county not in self._county_zone:
            try:
                response = client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
                response.raise_for_status()
                self._county_zone[county] = response.json()["properties"]["forecastZone"].rsplit("/", 1)[-1]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return None                          # not remembered: tried again next update
        return self._county_zone[county]

    def _zone(self, client, url: str) -> list:
        if url not in self._zones:
            response = client.get(url)
            response.raise_for_status()
            self._zones[url] = _rings(response.json().get("geometry"))
        return self._zones[url]


class IemStormReportsProcessor(FeedProcessor):
    """National Weather Service Local Storm Reports (what spotters, police, cameras and social
    media saw: flooding, hail, wind damage...), from Iowa State's mesonet. No url: the reader
    asks for the last `hours` (default 24) of reports from NWS office `office` (e.g. "MFL",
    Miami) itself, because the service's own "last N hours" option drops the office filter.
    One row per report, titled "Flash flood: 2 W Hollywood", at the spot it was seen.

    Its link is a web search for the report's remark, county and state, to find the news story
    or post it came from. Optional setting `search`: the search address, with {query} where
    the words go (default Google). A report with no remark links to the NWS report itself."""

    URL = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
    SEARCH = "https://www.google.com/search?q={query}"

    def _link(self, p: dict, remark: str) -> str | None:
        if remark:
            words = " ".join(w for w in [remark, p.get("county") and f"{p['county']} County", p.get("state") or p.get("st")] if w)
            return self.settings.get("search", self.SEARCH).replace("{query}", quote_plus(words))
        return f"https://mesonet.agron.iastate.edu/p.php?pid={p['product_id']}" if p.get("product_id") else None

    def process(self, client, response) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        start = now - pd.Timedelta(hours=self.settings.get("hours", 24))
        fmt = "%Y-%m-%dT%H:%MZ"
        response = client.get(f"{self.URL}?wfos={self.settings['office']}&sts={start.strftime(fmt)}&ets={now.strftime(fmt)}")
        response.raise_for_status()
        rows = []
        for report in response.json().get("features", []):
            p = report.get("properties") or {}
            lon, lat = ((report.get("geometry") or {}).get("coordinates") or [None, None])[:2]
            magnitude = p.get("magf") if isinstance(p.get("magf"), (int, float)) else None   # hail inches, gust mph, rain inches...
            size = f"{magnitude:g} {p.get('unit') or ''}".strip() if magnitude is not None else None
            remark = " ".join((p.get("remark") or "").split())     # the remarks come with line breaks and runs of spaces
            rows.append({
                "title": f"{(p.get('typetext') or 'Report').capitalize()}: {p.get('city') or ''}".rstrip(": "),
                "url": self._link(p, remark),
                "source": self.settings["name"],
                "time": pd.to_datetime(p.get("valid"), utc=True) if p.get("valid") else None,
                "lat": lat, "lon": lon,
                "id": f"{p.get('valid')} {lat} {lon} {p.get('typetext')}",
                "magnitude": magnitude,
                "details": " · ".join(f for f in [size, p.get("county") and f"{p['county']} County", remark] if f),
                "geometry": [],
            })
        return pd.DataFrame(rows, columns=WEATHER_COLUMNS)


# --- flights: adsb.fi's open data (live aircraft positions from volunteers' ADS-B receivers) -----

FLIGHT_COLUMNS = EVENT_COLUMNS + ["heading", "emergency", "flags"]
EMERGENCY_SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "emergency"}
ADSB_FLAGS = {"military": 1, "interesting": 2, "pia": 4, "ladd": 8}             # adsb.fi's dbFlags bits
FLAG_WORDS = {"military": "military", "interesting": "interesting", "pia": "privacy (PIA)", "ladd": "limited tracking (LADD)"}


class AdsbFiProcessor(FeedProcessor):
    """Live aircraft from adsb.fi (no url, no account). The settings say what to ask for;
    use any of them together, and the answers are merged (each plane once):
      near:          {lat, lon, nm}   every plane within nm nautical miles (at most 250)
      military:      true             military aircraft, worldwide
      squawks:       [7500, 7600, 7700]   planes squawking these codes, worldwide
      callsigns:     [JBU2926, ...]   flights by callsign (a watch list)
      registrations: [N972QS, ...]    aircraft by tail number (a watch list)
      area:          [south, west, north, east]   keep only planes inside this box
      flags:         [military, interesting, pia, ladd]   keep only planes adsb.fi flags so
                     (interesting = on its list of notable aircraft, often government;
                     pia/ladd = owners who asked to hide their identity or limit tracking)
    One row per plane, titled "JBU2926 · A320 · 8,500 ft", at its position, with its heading.
    adsb.fi asks for at most one request a second, so requests (from every flight feed) are
    spaced out."""

    BASE = "https://opendata.adsb.fi/api/v2"
    _lock = threading.Lock()
    _last = 0.0

    def process(self, client, response) -> pd.DataFrame:
        s = self.settings
        paths = []
        if s.get("near"):
            near = s["near"]
            paths.append(f"/lat/{near['lat']}/lon/{near['lon']}/dist/{min(float(near.get('nm', 25)), 250):g}")
        if s.get("military"):
            paths.append("/mil")
        paths += [f"/sqk/{code}" for code in s.get("squawks") or []]
        paths += [f"/callsign/{c}" for c in s.get("callsigns") or []]
        paths += [f"/registration/{r}" for r in s.get("registrations") or []]
        planes = {}
        for path in paths:
            for plane in self._get(client, path):
                planes.setdefault(plane.get("hex"), plane)
        area = s.get("area")
        wanted = sum(ADSB_FLAGS[f] for f in s.get("flags") or [] if f in ADSB_FLAGS)
        rows = [self._row(p) for p in planes.values() if p.get("lat") is not None and p.get("lon") is not None
                and (not area or (area[0] <= p["lat"] <= area[2] and area[1] <= p["lon"] <= area[3]))
                and (not wanted or (p.get("dbFlags") or 0) & wanted)]
        return pd.DataFrame(rows, columns=FLIGHT_COLUMNS)

    def _get(self, client, path: str) -> list[dict]:
        with AdsbFiProcessor._lock:                      # one request a second, across every flight feed
            wait = AdsbFiProcessor._last + 1.1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            response = client.get(self.BASE + path)
            AdsbFiProcessor._last = time.monotonic()
        response.raise_for_status()
        data = response.json()
        return data.get("ac") or data.get("aircraft") or []

    def _row(self, p: dict) -> dict:
        name = (p.get("flight") or "").strip() or p.get("r") or p.get("hex")
        altitude = p.get("alt_baro")
        height = "on the ground" if altitude == "ground" else f"{altitude:,} ft" if isinstance(altitude, (int, float)) else None
        squawk = str(p.get("squawk") or "")
        trouble = EMERGENCY_SQUAWKS.get(squawk) or (p.get("emergency") if p.get("emergency") not in (None, "none") else None)
        facts = [p.get("desc") or p.get("t"), p.get("ownOp"), p.get("r") if p.get("r") != name else None,
                 f"{p['gs']:.0f} kt" if isinstance(p.get("gs"), (int, float)) else None,
                 f"squawk {squawk}" + (f" ({trouble})" if trouble else "") if squawk else None,
                 *[FLAG_WORDS[flag] for flag, bit in ADSB_FLAGS.items() if (p.get("dbFlags") or 0) & bit]]
        return {
            "title": " · ".join(f for f in [name, p.get("t"), height] if f),
            "url": f"https://globe.adsb.fi/?icao={p.get('hex')}",     # its live track
            "source": self.settings["name"],
            "time": datetime.now(timezone.utc),
            "lat": p.get("lat"), "lon": p.get("lon"),
            "id": p.get("hex"),
            "magnitude": None,
            "details": " · ".join(f for f in facts if f),
            "heading": p.get("track") if isinstance(p.get("track"), (int, float)) else p.get("true_heading"),
            "emergency": bool(trouble),
            "flags": " ".join(flag for flag, bit in ADSB_FLAGS.items() if (p.get("dbFlags") or 0) & bit),   # e.g. "military"
        }


# --- rain radar: Iowa State mesonet's US NEXRAD composite ----------------------------------------

IMAGE_COLUMNS = COLUMNS + ["id", "details", "image", "bounds"]


class IemRadarProcessor(FeedProcessor):
    """The US rain radar composite (NEXRAD base reflectivity, refreshed every 5 minutes), in
    the same flat lat/lon projection as the maps, e.g.
    url https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/n0r_0.png
    (its world file, the same name ending .wld, says where it lies). Setting `area`
    [south, west, north, east]: the box cut out, so the page only loads what it shows.
    One row: the cut-out as PNG bytes in `image` (no rain = see-through) and its exact
    edges in `bounds` [south, west, north, east]."""

    def process(self, client, response) -> pd.DataFrame:
        world = client.get(self.settings["url"].rsplit(".", 1)[0] + ".wld")
        world.raise_for_status()
        size_x, _, _, size_y, centre_x, centre_y = (float(v) for v in world.text.split())   # size_y is negative
        west, north = centre_x - size_x / 2, centre_y - size_y / 2                          # the top-left pixel's corner
        image = Image.open(io.BytesIO(response.content))
        south_edge, west_edge, north_edge, east_edge = self.settings.get("area") or [
            north + size_y * image.height, west, north, west + size_x * image.width]
        left = max(0, int((west_edge - west) / size_x))
        top = max(0, int((north - north_edge) / -size_y))
        right = min(image.width, int(-(-(east_edge - west) // size_x)))
        bottom = min(image.height, int(-(-(north - south_edge) // -size_y)))
        cut = image.crop((left, top, right, bottom))
        out = io.BytesIO()
        cut.save(out, "PNG", optimize=True, transparency=0)        # palette entry 0 (black) is "no rain"
        made = image.info.get("gentime")
        return pd.DataFrame([{
            "title": self.settings["name"],
            "url": None,
            "source": self.settings["name"],
            "time": pd.to_datetime(made, utc=True) if made else datetime.now(timezone.utc),   # e.g. "2026-11-18T17:14:04Z"
            "lat": None, "lon": None,
            "id": made,
            "details": "NEXRAD base reflectivity, Iowa State mesonet",
            "image": out.getvalue(),
            "bounds": [north + size_y * bottom, west + size_x * left, north + size_y * top, west + size_x * right],
        }], columns=IMAGE_COLUMNS)


# --- world pictures from NASA's satellite map service (GIBS) ---------------------------------------

class GibsWmsProcessor(FeedProcessor):
    """One picture of the whole world from NASA GIBS, in the maps' flat lat/lon projection,
    newest available (no account). Settings:
      layers     GIBS layer names drawn together, e.g. [IMERG_Precipitation_Rate_30min]
                 (satellite rainfall, every 30 minutes, about 6 hours behind) or the GOES-West,
                 GOES-East and Himawari "Band13_Clean_Infrared" cloud pictures (every 10 minutes)
      width, height   picture size (default 2048 x 1024, about 0.18° a pixel)
      drop_grey  true: grey pixels become see-through, so a colour-enhanced cloud picture
                 shows only its coloured (cold, stormy) cloud tops
    One row: the picture as PNG bytes in `image`, the whole world in `bounds`."""

    WMS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self._last_good: datetime | None = None          # the newest frame found with something in it

    def _get(self, client, width: int, height: int, when: datetime | None) -> np.ndarray:
        s = self.settings
        params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": ",".join(s["layers"]), "STYLES": "",
                  "CRS": "EPSG:4326", "BBOX": "-90,-180,90,180", "WIDTH": width, "HEIGHT": height,
                  "FORMAT": "image/png", "TRANSPARENT": "TRUE"}
        if when:
            params["TIME"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = client.get(s.get("url") or self.WMS, params=params)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"GIBS answered {r.headers.get('content-type')}: {r.text[:200]}")   # an error report, not a picture
        return np.asarray(Image.open(io.BytesIO(r.content)).convert("RGBA")).copy()

    def _newest_with_data(self, client) -> datetime | None:
        """Layers whose newest frames are announced before they're filled (satellite rainfall)
        set `step_minutes`: try tiny versions, newest first, back to the last one that had
        data (or `look_back_hours`, default 12), and use the first that isn't empty."""
        step = self.settings.get("step_minutes")
        if not step:
            return None                                   # the service's newest is fine
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        when = now - pd.Timedelta(minutes=now.minute % step)
        oldest = self._last_good or now - pd.Timedelta(hours=self.settings.get("look_back_hours", 12))
        while when >= oldest:
            if self._get(client, 64, 32, when)[..., 3].any():
                self._last_good = when
                return when
            when -= pd.Timedelta(minutes=step)
        return self._last_good

    def process(self, client, response) -> pd.DataFrame:
        s = self.settings
        when = self._newest_with_data(client)
        picture = self._get(client, s.get("width", 2048), s.get("height", 1024), when)
        if s.get("drop_grey"):
            rgb = picture[..., :3].astype(np.int16)
            grey = (rgb.max(axis=-1) - rgb.min(axis=-1)) < 30                  # no colour: warm ground, low cloud
            picture[grey, 3] = 0
        picture[picture[..., 3] == 0] = 0                                      # see-through pixels all alike: a much smaller file
        out = io.BytesIO()
        Image.fromarray(picture).save(out, "PNG", optimize=True)
        data = out.getvalue()
        return pd.DataFrame([{
            "title": s["name"],
            "url": "https://worldview.earthdata.nasa.gov/",
            "source": s["name"],
            "time": when or datetime.now(timezone.utc),
            "lat": None, "lon": None,
            "id": hashlib.md5(data).hexdigest()[:12],                         # changes only when the picture does
            "details": "NASA GIBS: " + ", ".join(s["layers"]) + (f", {when:%H:%M} UTC" if when else ""),
            "image": data,
            "bounds": [-90.0, -180.0, 90.0, 180.0],
        }], columns=IMAGE_COLUMNS)


# --- global rain: RainViewer's radar mosaic -------------------------------------------------------

TILE_COLUMNS = COLUMNS + ["id", "details", "tiles"]


class RainViewerProcessor(FeedProcessor):
    """Where it's raining now, worldwide wherever there is ground radar (blank over open
    ocean): RainViewer's free radar mosaic, a new frame every 10 minutes, e.g.
    url https://api.rainviewer.com/public/weather-maps.json
    One row: the newest frame's tile address in `tiles`, with {z}/{x}/{y} for the page to fill
    in (web-map tiles, 256 px). The page loads the tiles it needs straight from RainViewer."""

    def process(self, client, response) -> pd.DataFrame:
        maps = response.json()
        newest = maps["radar"]["past"][-1]
        made = datetime.fromtimestamp(newest["time"], timezone.utc)
        return pd.DataFrame([{
            "title": self.settings["name"],
            "url": "https://www.rainviewer.com/",
            "source": self.settings["name"],
            "time": made,
            "lat": None, "lon": None,
            "id": str(newest["time"]),
            "details": f"RainViewer radar mosaic, {made:%H:%M} UTC",
            "tiles": f"{maps['host']}{newest['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png",   # colour scheme 2, smoothed, snow shown
        }], columns=TILE_COLUMNS)


# --- news by place: GDELT 2.0 event files ---------------------------------------------------------

GDELT_LAST = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_FILE = "http://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
GDELT_KINDS = {"01": "statement", "02": "appeal", "03": "intent to cooperate", "04": "consultation",
               "05": "diplomatic cooperation", "06": "material cooperation", "07": "aid", "08": "concession",
               "09": "investigation", "10": "demand", "11": "disapproval", "12": "rejection", "13": "threat",
               "14": "protest", "15": "show of force", "16": "reduced relations", "17": "coercion",
               "18": "assault", "19": "fight", "20": "mass violence"}


def _headline_from_link(url: str) -> str | None:
    """'https://site/news/2026-11-18/immigrant-advocates-implore-doral-council.html' ->
    'Immigrant advocates implore doral council'. None when the link has no words in it."""
    path = httpx.URL(url).path.rstrip("/")
    for part in reversed(path.split("/")):
        words = [w for w in part.rsplit(".", 1)[0].replace("_", "-").split("-") if w and not w.isdigit()]
        if len(words) >= 3 and sum(len(w) for w in words) > 12:
            return " ".join(words).capitalize()
    return None


class GdeltEventsProcessor(FeedProcessor):
    """News by place: GDELT reads the world's news and publishes, every 15 minutes, the
    events it found with where they happened and an article link. No url. Settings:
      area   [south, west, north, east]   keep events located inside this box
      hours  how long an event stays (default 6); at start the earlier files are fetched too
    One row per article and place, titled "Miami: Nashville man arrested after dna evidence
    links...", at the place, with the kind of event and how many articles mention it."""

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self._seen: set[str] = set()                 # files already read
        self._events: dict[tuple, dict] = {}         # (link, place) -> row

    def process(self, client, response) -> pd.DataFrame:
        hours = self.settings.get("hours", 6)
        latest = client.get(GDELT_LAST)
        latest.raise_for_status()
        newest = next(line.split()[2] for line in latest.text.splitlines() if line.endswith(".export.CSV.zip"))
        stamp = newest.rsplit("/", 1)[1].split(".")[0]                       # 20261118171500
        end = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        start = end - pd.Timedelta(hours=hours)
        when = end
        while when > start:                                                  # this file and the earlier ones in the window
            name = when.strftime("%Y%m%d%H%M%S")
            if name not in self._seen:
                self._read(client, GDELT_FILE.format(stamp=name))
                self._seen.add(name)
            when -= pd.Timedelta(minutes=15)
        self._events = {k: e for k, e in self._events.items() if e["time"] > start}
        return pd.DataFrame(list(self._events.values()), columns=EVENT_COLUMNS)

    def _read(self, client, url: str) -> None:
        response = client.get(url)
        if response.status_code == 404:                                     # GDELT skips a file now and then
            return
        response.raise_for_status()
        area = self.settings.get("area")
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
        for line in text.splitlines():
            f = line.split("\t")
            if len(f) < 61 or not f[56] or not f[57]:
                continue
            lat, lon = float(f[56]), float(f[57])
            if area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3]):
                continue
            link, place = f[60], f[52]
            mentions = int(f[31] or 0)
            key = (link, place)
            if key in self._events and self._events[key]["magnitude"] >= mentions:
                continue
            town = place.split(",")[0]
            headline = _headline_from_link(link) or httpx.URL(link).host
            self._events[key] = {
                "title": f"{town}: {headline}",
                "url": link,
                "source": self.settings["name"],
                "time": datetime.strptime(f[59], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc),
                "lat": lat, "lon": lon,
                "id": f[0],
                "magnitude": mentions,                                      # how widely it's reported
                "details": " · ".join([GDELT_KINDS.get(f[28], "event"), f"{mentions} mentions", place, httpx.URL(link).host]),
            }


# --- FAA: temporary flight restrictions and airport status -------------------------------------

class FaaTfrProcessor(FeedProcessor):
    """Temporary flight restrictions (TFRs): no-fly areas for VIP movements, security, wildfires,
    stadiums, launches... e.g. url https://tfr.faa.gov/tfrapi/exportTfrList (the current list).
    Their shapes come from the FAA's map service, which also keeps some old ones, so only
    shapes whose NOTAM is in the current list are used. Optional `area` [south, west, north,
    east]. One row per TFR, titled "TFR 6/8638 Security: Palm Beach, FL", with its area."""

    SHAPES = ("https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature"
              "&typeName=TFR:V_TFR_LOC&maxFeatures=1000&outputFormat=application/json&srsname=EPSG:4326")
    DETAIL = "https://tfr.faa.gov/tfr3/?page=detail_{id}"

    def process(self, client, response) -> pd.DataFrame:
        current = {t["notam_id"]: t for t in response.json()}
        shapes = client.get(self.settings.get("shapes", self.SHAPES))
        shapes.raise_for_status()
        rings, titles = {}, {}
        for feature in shapes.json().get("features", []):
            p = feature.get("properties") or {}
            notam = (p.get("NOTAM_KEY") or "").split("-")[0]           # "5/6914-1-FDC-F" -> "5/6914"
            if notam in current:
                rings.setdefault(notam, []).extend(_rings(feature.get("geometry")))
                titles.setdefault(notam, (p.get("TITLE") or "").strip(), )
        area = self.settings.get("area")
        rows = []
        for notam, t in current.items():
            lat, lon = _middle(rings.get(notam, []))
            if area and (lat is None or not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            kind = (t.get("type") or "").title()
            place = titles.get(notam) or " ".join((t.get("description") or "").split())
            rows.append({
                "title": f"TFR {notam} {kind}: {place}",
                "url": self.DETAIL.format(id=notam.replace("/", "_")),
                "source": self.settings["name"],
                "time": pd.to_datetime(t.get("creation_date"), format="%m/%d/%Y", utc=True) if t.get("creation_date") else None,
                "lat": lat, "lon": lon,
                "id": notam,
                "magnitude": None,
                "details": " · ".join(f for f in [kind, t.get("facility") and f"{t['facility']} center", t.get("state")] if f),
                "geometry": [("polygon", ring) for ring in rings.get(notam, [])],
            })
        return pd.DataFrame(rows, columns=WEATHER_COLUMNS)


class FaaAirportStatusProcessor(FeedProcessor):
    """FAA airport status, e.g. url https://nasstatus.faa.gov/api/airport-status-information:
    ground stops, ground delay programs, arrival/departure delays and closure notices. One row
    per airport and kind, titled "SFO: ground stop (wind) until 12:00 pm PST", at the airport
    (looked up once from weather.gov's station list). Optional `area`."""

    _airports: dict[str, tuple | None] = {}        # code -> (lat, lon), shared by every copy of the feed

    def process(self, client, response) -> pd.DataFrame:
        root = ET.fromstring(response.content)
        events = []
        for p in root.iter("Program"):
            events.append((p, "ground stop", f"ground stop ({p.findtext('Reason')}) until {p.findtext('End_Time')}"))
        for p in root.iter("Ground_Delay"):
            events.append((p, "ground delays", f"ground delays, average {p.findtext('Avg')}, up to {p.findtext('Max')} ({p.findtext('Reason')})"))
        for p in root.iter("Delay"):
            for d in p.iter("Arrival_Departure"):
                events.append((p, f"{d.get('Type', '').lower()} delays",
                               f"{d.get('Type', '').lower()} delays {d.findtext('Min')} to {d.findtext('Max')}, {(d.findtext('Trend') or '').lower()} ({p.findtext('Reason')})"))
        for p in root.iter("Airport"):
            events.append((p, "closure", f"closure notice until {(p.findtext('Reopen') or '').rstrip('.')}"))
        area = self.settings.get("area")
        made = root.findtext("Update_Time")
        rows = []
        for element, kind, text in events:
            code = element.findtext("ARPT")
            if not code:
                continue
            lat, lon = self._place(client, code) or (None, None)
            if area and (lat is None or not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            rows.append({
                "title": f"{code}: {text}",
                "url": "https://nasstatus.faa.gov/",
                "source": self.settings["name"],
                "time": pd.to_datetime(made, format="%a %b %d %H:%M:%S %Y GMT", utc=True) if made else datetime.now(timezone.utc),
                "lat": lat, "lon": lon,
                "id": f"{kind}:{code}",
                "magnitude": None,
                "details": " ".join((element.findtext("Reason") or "").split())[:200],
                "geometry": [],
            })
        return pd.DataFrame(rows, columns=WEATHER_COLUMNS)

    def _place(self, client, code: str) -> tuple | None:
        if code not in self._airports:
            self._airports[code] = None
            # mainland K, Alaska / Hawaii / Pacific P, Puerto Rico TJ + the first two letters (SJU -> TJSJ)
            for station in ([code] if len(code) == 4 else [f"K{code}", f"P{code}", f"TJ{code[:2]}"]):
                try:
                    r = client.get(f"https://api.weather.gov/stations/{station}")
                    if r.status_code == 200:
                        lon, lat = r.json()["geometry"]["coordinates"][:2]
                        self._airports[code] = (lat, lon)
                        break
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    break                                  # not remembered as found; tried again next run
        return self._airports[code]


# --- cyber: CISA's Known Exploited Vulnerabilities catalog ----------------------------------------

class CisaKevProcessor(FeedProcessor):
    """Vulnerabilities attackers are using now, from CISA's catalog, e.g. url
    https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
    (1.8 MB: update daily). Setting `newest` (default 20): how many of the latest additions.
    One row each, titled "CVE-2026-12345: Perforce Puppet Enterprise Deserialization...". No place."""

    def process(self, client, response) -> pd.DataFrame:
        entries = sorted(response.json().get("vulnerabilities", []), key=lambda v: v.get("dateAdded", ""), reverse=True)
        rows = []
        for v in entries[: self.settings.get("newest", 20)]:
            facts = [f"{v.get('vendorProject')} {v.get('product')}".strip(), f"fix by {v['dueDate']}" if v.get("dueDate") else None,
                     "used in ransomware" if v.get("knownRansomwareCampaignUse") == "Known" else None]
            rows.append({
                "title": f"{v.get('cveID')}: {v.get('vulnerabilityName')}",
                "url": f"https://nvd.nist.gov/vuln/detail/{v.get('cveID')}",
                "source": self.settings["name"],
                "time": pd.to_datetime(v.get("dateAdded"), utc=True) if v.get("dateAdded") else None,
                "lat": None, "lon": None,
                "id": v.get("cveID"),
                "magnitude": None,
                "details": " · ".join(f for f in facts if f),
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- internet outages: Georgia Tech's IODA -------------------------------------------------------

class _OnceASecond:
    """Spaces out requests to a service that asks for at most one a second."""
    def __init__(self) -> None:
        self.lock, self.last = threading.Lock(), 0.0
    def get(self, client, url, **kwargs):
        with self.lock:
            wait = self.last + 1.1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            response = client.get(url, **kwargs)
            self.last = time.monotonic()
        return response


NOMINATIM = _OnceASecond()                     # OpenStreetMap's place search: one request a second


class IodaOutagesProcessor(FeedProcessor):
    """Internet outages by country over the last `hours` (default 24), from Georgia Tech's
    IODA (no url, no account). Optional `min_score` (default 0) leaves out small ones.
    IODA gives no positions, so each country is placed at its centre, looked up once from
    OpenStreetMap. One row per country, titled "Internet outage: Iran"."""

    API = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/summary"
    _places: dict[str, tuple | None] = {}

    def process(self, client, response) -> pd.DataFrame:
        now = datetime.now(timezone.utc)
        hours = self.settings.get("hours", 24)
        r = client.get(f"{self.API}?from={int(now.timestamp()) - hours * 3600}&until={int(now.timestamp())}&entityType=country&limit=100")
        r.raise_for_status()
        rows = []
        for outage in r.json().get("data") or []:
            entity = outage.get("entity") or {}
            score = (outage.get("scores") or {}).get("overall") or 0
            if score < self.settings.get("min_score", 0):
                continue
            code = entity.get("code")
            lat, lon = self._place(client, code) or (None, None)
            rows.append({
                "title": f"Internet outage: {entity.get('name')}",
                "url": f"https://ioda.inetintel.cc.gatech.edu/country/{code}",
                "source": self.settings["name"],
                "time": now,
                "lat": lat, "lon": lon,
                "id": code,
                "magnitude": float(score),
                "details": f"{outage.get('event_cnt', 0)} outage events in the last {hours} h · score {score:,.0f}",
            })
        rows.sort(key=lambda row: -row["magnitude"])                    # worst first
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)

    def _place(self, client, code: str | None) -> tuple | None:
        if code and code not in self._places:
            try:
                r = NOMINATIM.get(client, "https://nominatim.openstreetmap.org/search",
                                  params={"country": code, "format": "json", "limit": 1, "featureType": "country"})
                found = r.json()
                self._places[code] = (float(found[0]["lat"]), float(found[0]["lon"])) if found else None
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                return None                                             # tried again next update
        return self._places.get(code)


# --- satellites: CelesTrak orbits, positions now and visible passes ---------------------------------

EARTH_RADIUS_KM = 6371.0


def _gmst(jd: np.ndarray) -> np.ndarray:
    """Greenwich sidereal angle (radians) for Julian dates: how far Earth has turned."""
    t = (jd - 2451545.0) / 36525.0
    degrees = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t
    return np.radians(degrees % 360)


def _to_earth(teme: np.ndarray, jd: np.ndarray) -> np.ndarray:
    """Positions from the orbit maths' frame (TEME) to Earth-fixed x, y, z (km)."""
    g = _gmst(jd)
    x, y, z = teme[..., 0], teme[..., 1], teme[..., 2]
    return np.stack([x * np.cos(g) + y * np.sin(g), -x * np.sin(g) + y * np.cos(g), z], axis=-1)


def _unit_from_latlon(lat, lon) -> np.ndarray:
    lat, lon = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=-1)


class CelestrakSatellitesProcessor(FeedProcessor):
    """Satellites from CelesTrak (no url, no account). Settings:
      groups     CelesTrak groups, default [stations, visual] (the space stations and the
                 ~150 brightest satellites)
      observer   {lat, lon} for visible passes (default Fort Lauderdale)
      hours      how far ahead to look for passes (default 12)
      min_elevation   degrees above the horizon a pass must reach (default 10)
    One row per satellite at where it is now, titled "ISS (ZARYA) · 418 km · visible 6:42 PM,
    54° high" when it will be seen (the observer in darkness, the satellite in sunlight).
    Rows with a visible pass come first, soonest first."""

    GROUP = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
    STEP_SECONDS = 30

    def process(self, client, response) -> pd.DataFrame:
        from sgp4 import omm
        from sgp4.api import Satrec, SatrecArray
        records = {}
        for group in self.settings.get("groups", ["stations", "visual"]):
            r = client.get(self.GROUP.format(group=group))
            r.raise_for_status()
            for fields in r.json():
                records.setdefault(fields["NORAD_CAT_ID"], fields)            # in two groups: once
        names, sats = [], []
        for fields in records.values():
            sat = Satrec()
            omm.initialize(sat, {k: str(v) for k, v in fields.items()})
            names.append(fields["OBJECT_NAME"]); sats.append(sat)
        if not sats:
            return pd.DataFrame([], columns=EVENT_COLUMNS)
        now = datetime.now(timezone.utc)
        steps = int(self.settings.get("hours", 12) * 3600 / self.STEP_SECONDS) + 1
        when = np.array([now.timestamp() + i * self.STEP_SECONDS for i in range(steps)])
        jd_full = when / 86400.0 + 2440587.5
        jd, fr = np.floor(jd_full), jd_full - np.floor(jd_full)
        errors, teme, _ = SatrecArray(sats).sgp4(jd, fr)                       # (satellites, times, 3) km
        earth = _to_earth(teme, jd_full[None, :])
        radius = np.linalg.norm(earth, axis=-1)
        lat = np.degrees(np.arcsin(earth[..., 2] / radius))
        lon = np.degrees(np.arctan2(earth[..., 1], earth[..., 0]))

        obs = self.settings.get("observer") or {"lat": 26.12, "lon": -80.15}
        up = _unit_from_latlon(obs["lat"], obs["lon"])
        to_sat = earth - up * EARTH_RADIUS_KM
        elevation = np.degrees(np.arcsin(np.einsum("stk,k->st", to_sat, up) / np.linalg.norm(to_sat, axis=-1)))
        suns = np.array([subsolar_point(datetime.fromtimestamp(t, timezone.utc)) for t in when])
        sun = _unit_from_latlon(suns[:, 0], suns[:, 1])                       # (times, 3)
        dark_here = np.degrees(np.arcsin(sun @ up)) < -6                       # the observer's sky is dark
        along = np.einsum("stk,tk->st", earth, sun)
        lit = (along > 0) | (np.sqrt(np.maximum(radius ** 2 - along ** 2, 0)) > EARTH_RADIUS_KM)   # outside Earth's shadow
        seen = (elevation >= self.settings.get("min_elevation", 10)) & lit & dark_here[None, :] & (errors == 0)

        rows = []
        for i, name in enumerate(names):
            if errors[i, 0]:
                continue
            height = radius[i, 0] - EARTH_RADIUS_KM
            title, start, details = f"{name} · {height:,.0f} km", None, "not visible from here in the next hours"
            visible = np.flatnonzero(seen[i])
            if visible.size:
                first = visible[0]
                end = first
                while end + 1 < steps and seen[i, end + 1]:
                    end += 1
                peak = first + int(np.argmax(elevation[i, first:end + 1]))
                start = datetime.fromtimestamp(when[first], timezone.utc)
                local = start.astimezone()
                title += f" · visible {local:%I:%M %p}".replace(" 0", " ") + f", {elevation[i, peak]:.0f}° high"
                details = (f"visible from {local:%a %I:%M %p} for {(end - first + 1) * self.STEP_SECONDS / 60:.0f} min, "
                           f"highest {elevation[i, peak]:.0f}°")
            rows.append({
                "title": title,
                "url": f"https://www.n2yo.com/satellite/?s={sats[i].satnum}",
                "source": self.settings["name"],
                "time": start or now,
                "lat": float(lat[i, 0]), "lon": float(lon[i, 0]),
                "id": str(sats[i].satnum),
                "magnitude": float(height),
                "details": details,
                "_order": (0, when[visible[0]]) if visible.size else (1, name),
            })
        rows.sort(key=lambda row: row.pop("_order"))                            # visible passes first, soonest first
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- GPS jamming: gpsjam.org's daily map of aircraft reporting bad GPS -------------------------------

class GpsJamProcessor(FeedProcessor):
    """Where aircraft reported degraded GPS (jamming or spoofing) on the latest day gpsjam.org
    has published (usually yesterday), in hexagon cells. No url. Settings:
      threshold     share of aircraft with bad GPS to show a cell (default 0.1 = 10%)
      min_aircraft  aircraft a cell needs to count (default 3)
      area          optional [south, west, north, east]
    One row per cell, titled "GPS interference 45% (9 of 20 aircraft)", with its hexagon;
    worst first."""

    DATA = "https://gpsjam.org/data/{day}-h3_4.csv"

    def process(self, client, response) -> pd.DataFrame:
        import h3
        today = datetime.now(timezone.utc).date()
        for back in range(4):                                                  # today's is often not out yet
            day = (today - pd.Timedelta(days=back)).isoformat()
            r = client.get(self.DATA.format(day=day))
            if r.status_code == 200:
                break
        else:
            r.raise_for_status()
        threshold, least, area = self.settings.get("threshold", 0.1), self.settings.get("min_aircraft", 3), self.settings.get("area")
        rows = []
        for line in r.text.splitlines()[1:]:
            cell, good, bad = line.split(",")
            good, bad = int(good), int(bad)
            if good + bad < least or bad / (good + bad) < threshold:
                continue
            lat, lon = h3.cell_to_latlng(cell)
            if area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3]):
                continue
            share = bad / (good + bad)
            rows.append({
                "title": f"GPS interference {share:.0%} ({bad} of {good + bad} aircraft)",
                "url": f"https://gpsjam.org/?lat={lat:.2f}&lon={lon:.2f}&z=6&date={day}",
                "source": self.settings["name"],
                "time": pd.Timestamp(day, tz="UTC"),
                "lat": lat, "lon": lon,
                "id": cell,
                "magnitude": share,
                "details": f"{day} · {bad} of {good + bad} aircraft reported bad GPS",
                "geometry": [("polygon", [tuple(p) for p in h3.cell_to_boundary(cell)])],
            })
        rows.sort(key=lambda row: -row["magnitude"])
        return pd.DataFrame(rows, columns=WEATHER_COLUMNS)


# --- fires: NASA FIRMS satellite hotspots and NIFC's US wildfires ---------------------------------

class FirmsHotspotsProcessor(FeedProcessor):
    """Heat spots satellites saw in the last 24 hours (wildfires, also gas flares and farm burns),
    from NASA FIRMS's public CSV files (no key), e.g. url
    https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv
    (worldwide, about 10,000 spots). Settings:
      min_confidence  MODIS's 0-100 confidence a spot needs (default 80: about 3,000 worldwide)
      area            optional [south, west, north, east]
    One row per spot, titled "Fire hotspot: 37 MW, 98% confidence"; magnitude is the fire's
    radiative power (MW)."""

    def process(self, client, response) -> pd.DataFrame:
        spots = pd.read_csv(io.StringIO(response.text))
        least, area = self.settings.get("min_confidence", 80), self.settings.get("area")
        spots = spots[pd.to_numeric(spots["confidence"], errors="coerce").fillna(0) >= least]
        if area:
            spots = spots[spots["latitude"].between(area[0], area[2]) & spots["longitude"].between(area[1], area[3])]
        rows = []
        for s in spots.itertuples(index=False):
            hhmm = f"{int(s.acq_time):04d}"
            rows.append({
                "title": f"Fire hotspot: {s.frp:.0f} MW, {s.confidence}% confidence",
                "url": f"https://firms.modaps.eosdis.nasa.gov/map/#d:24hrs;@{s.longitude:.3f},{s.latitude:.3f},10.0z",
                "source": self.settings["name"],
                "time": pd.Timestamp(f"{s.acq_date} {hhmm[:2]}:{hhmm[2:]}", tz="UTC"),
                "lat": s.latitude, "lon": s.longitude,
                "id": f"{s.latitude:.4f},{s.longitude:.4f},{s.acq_date},{hhmm}",
                "magnitude": s.frp,
                "details": f"seen {s.acq_date} {hhmm[:2]}:{hhmm[2:]} UTC ({'night' if s.daynight == 'N' else 'day'}) · {s.brightness:.0f} K",
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


class NifcWildfiresProcessor(FeedProcessor):
    """US wildfires not yet declared out, from NIFC's current incidents (WFIGS) service, e.g. url
    https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query?where=IncidentTypeCategory%3D%27WF%27+AND+FireOutDateTime+IS+NULL&outFields=*&outSR=4326&f=json
    Optional `area`. One row per fire, titled "Aspen Acres fire: 102,007 acres, 96% contained",
    at its point of origin; magnitude is its size in acres."""

    def process(self, client, response) -> pd.DataFrame:
        area = self.settings.get("area")
        rows = []
        for f in response.json().get("features", []):
            a, g = f.get("attributes") or {}, f.get("geometry") or {}
            lat, lon = g.get("y"), g.get("x")
            if lat is None or lon is None or (area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            size, contained = a.get("IncidentSize"), a.get("PercentContained")
            facts = [f"{size:,.0f} acres" if size is not None else "size unknown",
                     f"{contained:.0f}% contained" if contained is not None else None]
            place = ", ".join(p for p in [f"{a['POOCounty']} County" if a.get("POOCounty") else None,
                                          (a.get("POOState") or "").removeprefix("US-") or None] if p)
            found = a.get("FireDiscoveryDateTime")
            rows.append({
                "title": f"{(a.get('IncidentName') or 'Unnamed').strip()} fire: " + ", ".join(x for x in facts if x),
                "url": "https://www.nifc.gov/fire-information/nfn",
                "source": self.settings["name"],
                "time": pd.to_datetime(a.get("ModifiedOnDateTime_dt") or found, unit="ms", utc=True) if (a.get("ModifiedOnDateTime_dt") or found) else None,
                "lat": lat, "lon": lon,
                "id": a.get("UniqueFireIdentifier") or a.get("IrwinID"),
                "magnitude": size,
                "details": " · ".join(x for x in [place, f"cause: {a['FireCause'].lower()}" if a.get("FireCause") else None,
                                                  f"found {pd.to_datetime(found, unit='ms', utc=True):%Y-%m-%d}" if found else None] if x),
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- floods: NOAA river gauges in flood ------------------------------------------------------------

class NwsRiverGaugesProcessor(FeedProcessor):
    """NOAA river gauges at a flood stage, from the National Weather Service's river gauges map
    service (no key), e.g. url
    https://mapservices.weather.noaa.gov/eventdriven/rest/services/water/riv_gauges/MapServer/0/query?where=status+IN+(%27minor%27,%27moderate%27,%27major%27)&outFields=*&outSR=4326&f=json
    (the `where` picks the stages: action, minor, moderate, major). Optional `area`.
    One row per gauge, titled "St. Johns River at Astor, FL: moderate flooding, 3.38 ft";
    magnitude is the stage, 1 (action) to 4 (major)."""

    STAGES = {"action": 1, "minor": 2, "moderate": 3, "major": 4}

    def process(self, client, response) -> pd.DataFrame:
        area = self.settings.get("area")
        rows = []
        for f in response.json().get("features", []):
            a = f.get("attributes") or {}
            lat, lon = a.get("latitude"), a.get("longitude")
            if lat is None or lon is None or (area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            status, units = (a.get("status") or "").lower(), a.get("units") or ""
            level = f", {a['observed']} {units}".rstrip() if a.get("observed") else ""
            place = " at ".join(p for p in [a.get("waterbody"), a.get("location")] if p) or "Gauge"
            place += f", {a['state']}" if a.get("state") else ""
            stage = "at action stage" if status == "action" else f"{status} flooding"
            marks = [f"{name} {a[key]} {units}".strip() for name, key in
                     [("flood", "flood"), ("moderate", "moderate"), ("major", "major")] if a.get(key)]
            rows.append({
                "title": f"{place}: {stage}{level}",
                "url": a.get("url"),
                "source": self.settings["name"],
                "time": pd.to_datetime(a["obstime"], utc=True) if a.get("obstime") else None,
                "lat": lat, "lon": lon,
                "id": a.get("gaugelid"),
                "magnitude": self.STAGES.get(status),
                "details": f"flood stages: {', '.join(marks)}" if marks else None,
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- traffic: Florida Highway Patrol's live incidents, and FL511 -----------------------------------

class FhpIncidentsProcessor(FeedProcessor):
    """Florida Highway Patrol's live dispatch list (crashes, disabled vehicles, debris, roads
    closed by water...) statewide, read from its public page (no key):
    url https://trafficincidents.flhsmv.gov/SmartWebClient/CadView.aspx
    Optional settings: `counties` (FHP's names, e.g. [MIAMI-DADE, BROWARD]) and `area`.
    One row per incident, titled "Vehicle crash w/injuries: S DIXIE HWY SB at SW 280TH ST, HOMESTEAD".

    The page gives times on a 12-hour clock without AM/PM, in Eastern time: each is taken as
    the latest of the two that isn't in the future."""

    ROW = re.compile(r'<tr[^>]*id="gvCAD_DXDataRow\d+"[^>]*>(.*?)</tr>', re.S)
    CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    INCIDENT = re.compile(r"popUP_MapInfo\('([^']+)'\)")
    TAG = re.compile(r"<[^>]+>")
    TIMEZONE = "America/New_York"

    @staticmethod
    def _text(cell: str) -> str:
        return " ".join(html.unescape(FhpIncidentsProcessor.TAG.sub(" ", cell)).split())

    @classmethod
    def _when(cls, text: str, now: pd.Timestamp) -> pd.Timestamp | None:
        try:
            clock = pd.Timestamp(datetime.strptime(text, "%m/%d/%Y %H:%M:%S"))
        except ValueError:
            return None
        first = clock - pd.Timedelta(hours=12) if clock.hour == 12 else clock          # 12:xx can be just past midnight or noon
        choices = [first, first + pd.Timedelta(hours=12)]
        local = [c.tz_localize(cls.TIMEZONE, ambiguous=True, nonexistent="shift_forward") for c in choices]
        past = [c for c in local if c <= now + pd.Timedelta(minutes=10)]
        return (max(past) if past else local[0]).tz_convert("UTC")

    @staticmethod
    def _place(location: str) -> str:
        """"S DIXIE HWY SB x[SW 280TH ST] [HOMESTEAD]" -> "S DIXIE HWY SB at SW 280TH ST, HOMESTEAD"."""
        cross = re.search(r"\bx\[(.*?)\]", location)
        city = re.findall(r"\[([^\[\]]*)\]\s*$", location)
        road = re.split(r"\s+x\[", location)[0]
        road = re.sub(r"\s*\[[^\]]*\]+\s*$", "", road) if cross else road
        out = road.strip()
        if cross and cross.group(1).strip() and cross.group(1).strip() not in out:
            out += f" at {cross.group(1).strip()}"
        if city and city[0].strip() and city[0].strip() not in out:
            out += f", {city[0].strip()}"
        return out

    def process(self, client, response) -> pd.DataFrame:
        counties = {c.upper() for c in self.settings.get("counties") or []}
        area = self.settings.get("area")
        now = pd.Timestamp.now(tz=self.TIMEZONE)
        rows = []
        for row in self.ROW.findall(response.text):
            cells = self.CELL.findall(row)
            if len(cells) < 9:
                continue
            kind, received, dispatched, arrived, county, location, remarks, lat, lon = [self._text(c) for c in cells[:9]]
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                continue
            if (counties and county.upper() not in counties) or (area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            incident = self.INCIDENT.search(cells[4])
            when = self._when(received, now)
            state = "arrived" if arrived else "dispatched" if dispatched else None
            rows.append({
                "title": f"{kind.capitalize()}: {self._place(location)}",
                "url": self.settings.get("url"),
                "source": self.settings["name"],
                "time": when,
                "lat": lat, "lon": lon,
                "id": incident.group(1) if incident else f"{received} {lat} {lon}",
                "magnitude": None,
                "details": " · ".join(x for x in [remarks.capitalize() if remarks else None,
                                                  f"{county.title()} County" if county else None,
                                                  f"troopers {state}" if state else None] if x),
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


class Fl511EventsProcessor(FeedProcessor):
    """Florida DOT's 511 traffic events, read from fl511.com's own pages (no key, no url): the
    Incidents List's data gives each event's details, and the map's icon lists give where each
    one is, matched by the event's id. Optional settings:
      types     the site's names (default [Incidents, Closures]; also Congestion,
                Disabled Vehicles, Construction Zones, Road Condition)
      counties  e.g. [Miami-Dade, Broward] (asked for one county at a time); none = statewide
      area      [south, west, north, east]
    One row per event, titled "Incident: I-95 Northbound", linking to its page on fl511.com
    (e.g. https://fl511.com/Event/Closures/461840?lang=en). Times are Eastern."""

    SITE = "https://fl511.com"
    PAGE = 100                                    # the most rows the list hands out at once
    TIMEZONE = "America/New_York"

    def _list(self, client, county: str | None) -> list[dict]:
        columns = [{"data": None, "name": ""}, {"name": "region"},
                   {"name": "county", **({"search": {"value": county}} if county else {})},
                   {"name": "roadwayName"}, {"name": "direction"}, {"name": "type"}, {"name": "severity"},
                   {"name": "description"}, {"name": "startDate"}, {"name": "lastUpdated"}, {"data": 10, "name": ""}]
        events, start = [], 0
        while True:
            query = {"columns": columns, "order": [{"column": 9, "dir": "desc"}], "start": start,
                     "length": self.PAGE, "search": {"value": ""}}
            response = client.get(f"{self.SITE}/List/GetData/traffic", params={"query": json.dumps(query), "lang": "en-US"})
            response.raise_for_status()
            answer = response.json()
            events += answer.get("data") or []
            start += self.PAGE
            if start >= (answer.get("recordsFiltered") or 0) or not answer.get("data"):
                return events

    def _places(self, client, layer: str) -> dict[str, list]:
        response = client.get(f"{self.SITE}/map/mapIcons/{layer}")
        response.raise_for_status()
        return {str(i.get("itemId")): i.get("location") for i in response.json().get("item2") or []}

    def _when(self, text: str | None) -> pd.Timestamp | None:
        try:
            return pd.Timestamp(datetime.strptime(text, "%m/%d/%y, %I:%M %p")).tz_localize(
                self.TIMEZONE, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")
        except (TypeError, ValueError):
            return None

    def process(self, client, response) -> pd.DataFrame:
        types = {t.lower() for t in self.settings.get("types") or ["Incidents", "Closures"]}
        area = self.settings.get("area")
        events = [e for county in (self.settings.get("counties") or [None]) for e in self._list(client, county)
                  if (e.get("type") or "").lower() in types]
        places = {}
        for layer in sorted({e.get("layerName") for e in events if e.get("layerName")}):
            places.update(self._places(client, layer))
        rows = []
        for e in events:
            lat, lon = (places.get(str(e.get("id"))) or [None, None])[:2]
            if lat is None or lon is None or (area and not (area[0] <= lat <= area[2] and area[1] <= lon <= area[3])):
                continue
            kind = " ".join(w.removesuffix("s") for w in (e.get("type") or "Event").split()).capitalize()
            road = " ".join(x for x in [e.get("roadwayName"), e.get("direction")] if x and x != "Unknown")
            full = " (full closure)" if e.get("isFullClosure") else ""
            description = re.sub(r"\s*Last updated at [^.]*\.\s*$", "", e.get("description") or "")
            rows.append({
                "title": f"{kind}: {road or 'road'}{full}",
                "url": f"{self.SITE}/Event/{e['layerName']}/{e['id']}?lang=en" if e.get("layerName") and e.get("id")
                       else f"{self.SITE}/map",                          # the event's own page: its details
                "source": self.settings["name"],
                "time": self._when(e.get("lastUpdated") or e.get("startDate")),
                "lat": lat, "lon": lon,
                "id": e.get("id"),
                "magnitude": None,
                "details": " · ".join(x for x in [description, e.get("laneDescription") and f"lanes: {e['laneDescription']}",
                                                  e.get("severity") and f"{e['severity'].lower()} severity"] if x),
            })
        return pd.DataFrame(rows, columns=EVENT_COLUMNS)


# --- ships: aisstream.io's live AIS stream ---------------------------------------------------------

SHIP_COLUMNS = EVENT_COLUMNS + ["heading"]
SHIP_STATUS = {0: "under way", 1: "at anchor", 2: "not under command", 3: "restricted manoeuvrability",
               4: "constrained by draught", 5: "moored", 6: "aground", 7: "fishing", 8: "sailing", 14: "emergency beacon"}


def _ship_type(code) -> str | None:
    """AIS ship type number -> a word (ITU-R M.1371 table 53)."""
    if not isinstance(code, int):
        return None
    named = {30: "fishing", 31: "towing", 32: "towing", 33: "dredger", 34: "dive vessel", 35: "military",
             36: "sailing", 37: "pleasure craft", 50: "pilot", 51: "search and rescue", 52: "tug",
             53: "port tender", 55: "law enforcement", 58: "medical"}
    if code in named:
        return named[code]
    return {4: "high-speed craft", 6: "passenger", 7: "cargo", 8: "tanker"}.get(code // 10)


class AisStreamProcessor(FeedProcessor):
    """Ships' AIS positions, live from aisstream.io's websocket (free; `key` is the API key from
    aisstream.io). No url. Settings:
      area             optional [south, west, north, east]; none = the whole world
      max_age_minutes  a ship not heard from for this long is dropped (default 30)
    The stream stays connected in the background, keeping each ship's latest position, name,
    type and destination; each update hands out what it holds. One row per ship, titled
    "MAERSK KENSINGTON: cargo, 14 kn"; magnitude is its speed (knots)."""

    URL = "wss://stream.aisstream.io/v0/stream"
    POSITIONS = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
    FIRST_WAIT = 15                      # seconds the first update waits for the stream to start

    def __init__(self, settings: dict) -> None:
        super().__init__(settings)
        self._ships: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._problem: str | None = None

    def subscription(self) -> dict:
        s, w, n, e = self.settings.get("area") or [-90, -180, 90, 180]
        return {"APIKey": self.settings.get("key"), "BoundingBoxes": [[[s, w], [n, e]]],
                "FilterMessageTypes": [*self.POSITIONS, "ShipStaticData", "StaticDataReport"]}

    def process(self, client, response) -> pd.DataFrame:
        if not self.settings.get("key"):
            raise ValueError("aisstream.io needs an API key: get one free at aisstream.io and set key: for this feed in config.yml")
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._listen, name=f"ais {self.settings['name']}", daemon=True)
            self._thread.start()
            end = time.monotonic() + self.FIRST_WAIT
            while time.monotonic() < end and not self._ships and not self._problem:
                time.sleep(0.2)
        if self._problem and not self._ships:
            raise ConnectionError(self._problem)
        return self.rows()

    def rows(self) -> pd.DataFrame:
        cutoff = time.time() - self.settings.get("max_age_minutes", 30) * 60
        with self._lock:
            for mmsi in [m for m, s in self._ships.items() if s["heard"] < cutoff]:
                del self._ships[mmsi]
            ships = [dict(s) for s in self._ships.values() if s.get("lat") is not None]
        rows = []
        for s in ships:
            speed = s.get("sog") if isinstance(s.get("sog"), (int, float)) and s["sog"] < 102.3 else None
            status = SHIP_STATUS.get(s.get("status"))
            moving = f"{speed:.0f} kn" if speed and speed >= 0.5 else status or "stopped"
            heading = s.get("heading") if isinstance(s.get("heading"), (int, float)) and s["heading"] < 360 else \
                s.get("cog") if isinstance(s.get("cog"), (int, float)) and s["cog"] < 360 else None
            rows.append({
                "title": f"{s.get('name') or 'MMSI ' + str(s['mmsi'])}: {s.get('type') or 'vessel'}, {moving}",
                "url": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{s['mmsi']}",
                "source": self.settings["name"],
                "time": pd.Timestamp(s["seen"], unit="s", tz="UTC"),
                "lat": s["lat"], "lon": s["lon"],
                "id": str(s["mmsi"]),
                "magnitude": speed,
                "details": " · ".join(x for x in [status, s.get("destination") and f"to {s['destination']}",
                                                  f"MMSI {s['mmsi']}"] if x),
                "heading": heading,
            })
        return pd.DataFrame(rows, columns=SHIP_COLUMNS)

    def _listen(self) -> None:
        from websockets.sync.client import connect
        wait = 5
        while True:
            try:
                with connect(self.URL, open_timeout=20, max_size=2 ** 22, compression="deflate") as ws:
                    ws.send(json.dumps(self.subscription()))       # must arrive within 3 seconds
                    for raw in ws:
                        message = json.loads(raw)
                        if "error" in message:
                            self._problem = f"aisstream.io: {message['error']}"
                            break
                        self.take(message)
                        self._problem, wait = None, 5
            except Exception as ex:                                 # anything: wait, then connect again
                self._problem = f"{type(ex).__name__}: {ex}"
            time.sleep(wait)
            wait = min(wait * 2, 300)

    def take(self, message: dict) -> None:
        """One stream message into the ship it's about."""
        kind = message.get("MessageType")
        body = (message.get("Message") or {}).get(kind) or {}
        meta = message.get("MetaData") or {}
        mmsi = meta.get("MMSI") or body.get("UserID")
        if not mmsi:
            return
        now = time.time()
        with self._lock:
            ship = self._ships.setdefault(mmsi, {"mmsi": mmsi, "heard": now})
            ship["heard"] = now
            name = (meta.get("ShipName") or "").strip(" @")
            if name:
                ship["name"] = name
            if kind in self.POSITIONS:
                lat, lon = body.get("Latitude"), body.get("Longitude")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and abs(lat) <= 90 and abs(lon) <= 180:
                    ship.update(lat=lat, lon=lon, sog=body.get("Sog"), cog=body.get("Cog"), seen=now,
                                heading=body.get("TrueHeading"), status=body.get("NavigationalStatus"))
            elif kind == "ShipStaticData":
                ship.update({k: v for k, v in {"name": (body.get("Name") or "").strip(" @"), "type": _ship_type(body.get("Type")),
                                               "destination": (body.get("Destination") or "").strip(" @")}.items() if v})
            elif kind == "StaticDataReport":
                a, b = body.get("ReportA") or {}, body.get("ReportB") or {}
                if a.get("Valid") and (a.get("Name") or "").strip(" @"):
                    ship["name"] = a["Name"].strip(" @")
                if b.get("Valid") and _ship_type(b.get("ShipType")):
                    ship["type"] = _ship_type(b.get("ShipType"))


class Feed:
    def __init__(self, name, settings):
        self.name = name
        self.settings = settings
        self.url = settings.get("url")
        self.update_minutes = settings.get("update_minutes", 60)
        self.etag = None

        self.processor = PROCESSORS[settings["format"]](settings)            # look up the class, then create one
        self.auth = AUTH_TYPES[settings.get("authtype", "none")](settings)

    def fetch(self, client: httpx.Client) -> pd.DataFrame | None:
        if not self.url:                                   # nothing to download (e.g. the sun):
            return self.processor.process(client, None)    # the processor calculates its items itself

        headers = {}
        if self.settings.get("check") == "etag" and self.etag:
            headers["If-None-Match"] = self.etag           # "only send it if it changed"
        # ** unpacks the auth's dictionary into named arguments: {"auth": x} becomes auth=x,
        # and NoAuth's {} adds nothing
        response = client.get(self.url, headers=headers, **self.auth.request_args())
        if response.status_code == 304:
            return None                                    # nothing changed since last time
        response.raise_for_status()
        self.etag = response.headers.get("ETag")
        return self.processor.process(client, response)



class NoAuth(AuthType):
    def request_args(self):
        return {}

class BasicAuth(AuthType):
    def request_args(self):
        return {"auth": httpx.BasicAuth(self.settings["id"], self.settings["pwd"])}

PROCESSORS = {                       # `format:` in the YAML -> the class that reads it
    "rss": RssProcessor,
    "sun": SunProcessor,
    "nhc_storms": NhcStormsProcessor,
    "nhc_track": NhcTrackProcessor,
    "nhc_cone": NhcConeProcessor,
    "usgs_quakes": UsgsQuakeProcessor,
    "nws_alerts": NwsAlertsProcessor,
    "iem_storm_reports": IemStormReportsProcessor,
    "adsb_fi": AdsbFiProcessor,
    "iem_radar": IemRadarProcessor,
    "rainviewer": RainViewerProcessor,
    "gibs_wms": GibsWmsProcessor,
    "gdelt_events": GdeltEventsProcessor,
    "faa_tfr": FaaTfrProcessor,
    "faa_airport_status": FaaAirportStatusProcessor,
    "cisa_kev": CisaKevProcessor,
    "ioda_outages": IodaOutagesProcessor,
    "celestrak_satellites": CelestrakSatellitesProcessor,
    "gpsjam": GpsJamProcessor,
    "firms_hotspots": FirmsHotspotsProcessor,
    "nifc_wildfires": NifcWildfiresProcessor,
    "nws_river_gauges": NwsRiverGaugesProcessor,
    "fhp_incidents": FhpIncidentsProcessor,
    "fl511_events": Fl511EventsProcessor,
    "aisstream": AisStreamProcessor,
}
AUTH_TYPES = {"none": NoAuth, "basic": BasicAuth}
