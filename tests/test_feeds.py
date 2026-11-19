import io
import zipfile

import httpx
import pandas as pd
from PIL import Image

from watchtower.feed_service import SharedDownloads, skip_stories
from watchtower.feeds import Feed

RSS2 = b"""<rss><channel><title>T</title><link>https://site.example/</link>
<image><url>https://site.example/logo.png</url></image>
<item><title>First</title><link>/a/1</link><pubDate>Fri, 06 Nov 2026 20:15:00 GMT</pubDate></item>
<item><title>Undated</title><link>https://other.example/b</link><pubDate>not a date</pubDate></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<link rel="alternate" href="https://atom.example/"/><icon>/favicon.png</icon>
<entry><title>Atom one</title><link rel="alternate" href="https://atom.example/1"/>
<updated>2026-11-11T17:04:56Z</updated></entry></feed>"""

RDF = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>R</title><link>https://rdf.example/</link></channel>
<item><title>RDF one</title><link>https://rdf.example/1</link><dc:date>2026-11-10T08:00:00Z</dc:date></item>
</rdf:RDF>"""


def client_serving(body: bytes, calls: list | None = None, etag: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"ETag": etag} if etag else {})
    return httpx.Client(transport=httpx.MockTransport(handler))


def news_feed(**extra) -> Feed:
    return Feed("news", {"name": "News", "url": "https://feed.example/rss", "format": "rss", **extra})


def test_rss2_items_links_and_dates():
    items = news_feed().fetch(client_serving(RSS2))
    assert list(items["title"]) == ["First", "Undated"]
    assert items["url"][0] == "https://site.example/a/1"        # relative to the site the feed names
    assert items["source"][0] == "News"
    assert items["time"][0] == pd.Timestamp("2026-11-06 20:15", tz="UTC")
    assert pd.isna(items["time"][1])                             # a bad date leaves the item undated


def test_atom_and_rdf():
    atom = news_feed().fetch(client_serving(ATOM))
    assert atom["title"][0] == "Atom one" and atom["url"][0] == "https://atom.example/1"
    rdf = news_feed().fetch(client_serving(RDF))
    assert rdf["title"][0] == "RDF one" and rdf["url"][0] == "https://rdf.example/1"


def test_etag_unchanged_returns_none():
    feed = news_feed(check="etag")
    client = client_serving(RSS2, etag='"v1"')
    assert feed.fetch(client) is not None
    assert feed.fetch(client) is None                            # 304: nothing changed


def test_skip_stories_any_case_title_only():
    data = pd.DataFrame({"title": ["[Sponsor] Buy", "Sponsored: x", "Real news", None],
                         "url": ["a", "b", "https://x/sponsor", "d"]})
    assert list(skip_stories(data, ["sponsor"])["title"].fillna("")) == ["Real news", ""]
    assert len(skip_stories(data, [])) == 4


ALERTS = {"features": [
    {"geometry": {"type": "Polygon", "coordinates": [[[-80.3, 26.0], [-80.1, 26.0], [-80.1, 26.2], [-80.3, 26.0]]]},
     "properties": {"@id": "https://api.weather.gov/alerts/a1", "id": "a1", "event": "Flash Flood Warning",
                    "areaDesc": "Broward, FL", "senderName": "NWS Miami FL", "sent": "2026-11-18T00:42:00-05:00",
                    "ends": "2026-11-18T03:45:00-05:00", "severity": "Severe", "urgency": "Immediate", "affectedZones": [],
                    "geocode": {"UGC": ["FLC011"], "SAME": ["012011"]}}},
    {"geometry": None,
     "properties": {"@id": "https://api.weather.gov/alerts/a2", "id": "a2", "event": "Flood Watch",
                    "areaDesc": "Inland Broward County", "senderName": "NWS Miami FL", "sent": "2026-11-18T02:21:00-05:00",
                    "affectedZones": ["https://api.weather.gov/zones/forecast/FLZ072"],
                    "geocode": {"UGC": ["FLZ072"], "SAME": ["012011"]}}},
    {"geometry": None,
     "properties": {"id": "a3", "event": "Rip Current Statement", "areaDesc": "Duval", "senderName": "NWS Jacksonville FL",
                    "affectedZones": ["https://api.weather.gov/zones/forecast/FLZ125"]}},
]}
ZONE = {"geometry": {"type": "MultiPolygon", "coordinates": [[[[-80.5, 26.1], [-80.3, 26.1], [-80.3, 26.3], [-80.5, 26.1]]]]}}


def test_nws_alerts_polygons_zones_and_sender():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if "/points/" in str(request.url):
            return httpx.Response(200, json={"properties": {"forecastZone": "https://api.weather.gov/zones/forecast/FLZ072"}})
        body = ZONE if "/zones/" in str(request.url) else ALERTS
        return httpx.Response(200, json=body)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = Feed("alerts", {"name": "NWS alerts (Miami)", "format": "nws_alerts",
                           "url": "https://api.weather.gov/alerts/active?area=FL", "sender": "NWS Miami FL"})
    items = feed.fetch(client)
    assert list(items["title"]) == ["Flash Flood Warning: Broward, FL", "Flood Watch: Inland Broward County"]   # Jacksonville left out
    warning, watch = items.iloc[0], items.iloc[1]
    assert warning["geometry"] == [("polygon", [(26.0, -80.3), (26.0, -80.1), (26.2, -80.1), (26.0, -80.3)])]
    assert round(warning["lat"], 2) == 26.05
    hazard = "https://forecast.weather.gov/showsigwx.php?warnzone=FLZ072&warncounty=FLC011&product1="
    assert warning["url"] == hazard + "Flash+Flood+Warning"          # a warning names only its county: zone looked up
    assert watch["url"] == hazard + "Flood+Watch"                    # a watch names its zone
    assert "Severe" in warning["details"] and "until" in warning["details"]
    assert watch["geometry"][0][0] == "polygon" and len(watch["geometry"][0][1]) == 4   # from the zone's outline
    feed.fetch(client)
    assert calls.count("https://api.weather.gov/zones/forecast/FLZ072") == 1           # zone outlines are kept
    assert not any("FLZ125" in c for c in calls)                                     # filtered before any zone lookup
    assert sum("/points/" in c for c in calls) == 1                                  # the county's zone is remembered


def test_storm_reports_last_hours_for_one_office():
    asked = []
    def handler(request):
        asked.append(request.url)
        return httpx.Response(200, json={"features": [
            {"geometry": {"type": "Point", "coordinates": [-80.19, 26.01]},
             "properties": {"typetext": "FLASH FLOOD", "city": "2 W Hollywood", "county": "Broward", "valid": "2026-11-18T05:30:00Z",
                            "magf": None, "unit": None, "remark": "Car wheels nearly\n            submerged.", "state": "FL",
                            "product_id": "202611180530-KMFL-NWUS52-LSRMFL"}},
            {"geometry": {"type": "Point", "coordinates": [-80.2, 26.0]},
             "properties": {"typetext": "HAIL", "city": "Hollywood", "county": "Broward", "valid": "2026-11-18T04:10:00Z",
                            "magf": 1.0, "unit": "INCH", "remark": "", "product_id": "202611180410-KMFL-NWUS52-LSRMFL"}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = Feed("reports", {"name": "NWS storm reports (Miami)", "format": "iem_storm_reports", "office": "MFL", "hours": 6}).fetch(client)
    params = asked[0].params
    assert params["wfos"] == "MFL" and "sts" in params and "ets" in params
    start, end = pd.Timestamp(params["sts"]), pd.Timestamp(params["ets"])
    assert end - start == pd.Timedelta(hours=6)
    assert list(items["title"]) == ["Flash flood: 2 W Hollywood", "Hail: Hollywood"]
    assert (items["lat"][0], items["lon"][0]) == (26.01, -80.19)
    assert items["url"][0] == "https://www.google.com/search?q=Car+wheels+nearly+submerged.+Broward+County+FL"   # find the story
    assert items["url"][1].endswith("pid=202611180410-KMFL-NWUS52-LSRMFL")        # no remark: the NWS report itself
    assert items["details"][0] == "Broward County · Car wheels nearly submerged."    # line breaks and runs of spaces collapsed
    assert items["details"][1] == "1 INCH · Broward County" and items["magnitude"][1] == 1.0


JETBLUE = {"hex": "a83e86", "flight": "JBU2926 ", "r": "N603JB", "t": "A320", "desc": "AIRBUS A-320", "ownOp": "JETBLUE AIRWAYS",
           "alt_baro": 8500, "gs": 280.4, "track": 45.0, "squawk": "2301", "lat": 26.28, "lon": -80.17, "emergency": "none"}
HELO = {"hex": "ae1234", "flight": "", "r": "93-26001", "t": "H60", "alt_baro": "ground", "squawk": "7700", "lat": 26.07,
        "lon": -80.15, "dbFlags": 1, "emergency": "general"}
FAR_AWAY = {"hex": "4ca123", "flight": "RYR1 ", "t": "B738", "alt_baro": 37000, "squawk": "7700", "lat": 54.0, "lon": 0.1}


def test_adsb_fi_merges_queries_filters_area_and_flags_emergencies(monkeypatch):
    monkeypatch.setattr("watchtower.feeds.time.sleep", lambda s: None)        # no one-second spacing in a test
    asked = []
    def handler(request):
        path = request.url.path
        asked.append(path)
        planes = {"/api/v2/lat/26.12/lon/-80.15/dist/25": [JETBLUE, HELO], "/api/v2/sqk/7700": [HELO, FAR_AWAY]}.get(path, [])
        return httpx.Response(200, json={"ac": planes})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = Feed("flights", {"name": "Flights", "format": "adsb_fi", "near": {"lat": 26.12, "lon": -80.15, "nm": 25},
                            "squawks": [7700], "area": [24, -88, 31, -79]})
    items = feed.fetch(client)
    assert asked == ["/api/v2/lat/26.12/lon/-80.15/dist/25", "/api/v2/sqk/7700"]
    assert list(items["title"]) == ["JBU2926 · A320 · 8,500 ft", "93-26001 · H60 · on the ground"]   # once each; Ryanair is outside the area
    jet, helo = items.iloc[0], items.iloc[1]
    assert jet["heading"] == 45.0 and not jet["emergency"] and jet["url"] == "https://globe.adsb.fi/?icao=a83e86"
    assert jet["details"] == "AIRBUS A-320 · JETBLUE AIRWAYS · N603JB · 280 kt · squawk 2301"
    assert helo["emergency"] and "squawk 7700 (emergency)" in helo["details"] and "military" in helo["details"]


def test_rainviewer_newest_frame_as_a_tile_address():
    index = {"host": "https://tilecache.rainviewer.com",
             "radar": {"past": [{"time": 1795020000, "path": "/v2/radar/old"}, {"time": 1795020600, "path": "/v2/radar/new"}]}}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=index)))
    items = Feed("rain", {"name": "Rain radar (world)", "format": "rainviewer",
                          "url": "https://api.rainviewer.com/public/weather-maps.json"}).fetch(client)
    assert items["tiles"][0] == "https://tilecache.rainviewer.com/v2/radar/new/256/{z}/{x}/{y}/2/1_1.png"
    assert items["id"][0] == "1795020600" and items["time"][0] == pd.Timestamp(1795020600, unit="s", tz="UTC")


def test_adsb_fi_watch_list_and_military():
    asked = []
    def handler(request):
        asked.append(request.url.path)
        return httpx.Response(200, json={"ac": []})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    Feed("w", {"name": "W", "format": "adsb_fi", "military": True, "callsigns": ["JBU2926"], "registrations": ["N972QS"]}).fetch(client)
    assert asked == ["/api/v2/mil", "/api/v2/callsign/JBU2926", "/api/v2/registration/N972QS"]


def test_radar_cut_to_the_area_with_no_rain_see_through():
    radar = Image.new("P", (100, 50), 0)                       # 1° pixels from 0°E 50°N: the world file below
    radar.putpalette([0, 0, 0, 0, 255, 0] + [0] * 762)
    radar.paste(1, (10, 10, 20, 20))                           # "rain" from 10-20°E, 30-40°N
    png = io.BytesIO(); radar.save(png, "PNG", pnginfo=None)
    world = "1.0\n0.0\n0.0\n-1.0\n0.5\n49.5\n"                  # pixel size, centre of the top-left pixel
    def handler(request):
        if request.url.path.endswith(".wld"):
            return httpx.Response(200, text=world)
        return httpx.Response(200, content=png.getvalue())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = Feed("radar", {"name": "Rain radar", "format": "iem_radar", "url": "https://radar.example/n0r_0.png",
                           "area": [25.0, 5.0, 45.0, 30.0]}).fetch(client)
    assert items["bounds"][0] == [25.0, 5.0, 45.0, 30.0]       # whole pixels inside the box
    cut = Image.open(io.BytesIO(items["image"][0]))
    assert cut.size == (25, 20) and cut.info.get("transparency") == 0      # no rain shows the map through
    assert cut.getpixel((5, 5)) == 1 and cut.getpixel((0, 0)) == 0         # the rain moved with the cut


GDELT_LINE = "\t".join(
    ["1290001"] + [""] * 25 + ["141", "14", "14", "", "-6.5", "12"] + [""] * 20
    + ["Miami, Florida, United States", "US", "", "", "25.77", "-80.19", "", "20261118171500",
       "https://news.example/local/2026-11-18/protest-at-county-hall-over-flooding.html"])
GDELT_FAR = "\t".join(["1290002"] + [""] * 25 + ["040", "04", "04", "", "1", "3"] + [""] * 20
                      + ["Paris, France", "FR", "", "", "48.85", "2.35", "", "20261118171500", "https://news.example/fr/a-b"])


def gdelt_zip(*lines) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("events.CSV", "\n".join(lines))
    return out.getvalue()


def test_gdelt_news_by_place_area_window_and_titles():
    asked = []
    def handler(request):
        url = str(request.url)
        asked.append(url.rsplit("/", 1)[1])
        if url.endswith("lastupdate.txt"):
            return httpx.Response(200, text="1 x http://data.gdeltproject.org/gdeltv2/20261118171500.export.CSV.zip\n")
        if "20261118171500" in url:
            return httpx.Response(200, content=gdelt_zip(GDELT_LINE, GDELT_FAR))
        return httpx.Response(404)                             # the earlier files: missing is fine
    client = httpx.Client(transport=httpx.MockTransport(handler))
    feed = Feed("news_place", {"name": "News by place", "format": "gdelt_events", "area": [24.0, -88.0, 31.5, -79.0], "hours": 1})
    items = feed.fetch(client)
    assert asked[1:] == ["20261118171500.export.CSV.zip", "20261118170000.export.CSV.zip",
                         "20261118164500.export.CSV.zip", "20261118163000.export.CSV.zip"]   # an hour of 15-minute files
    assert list(items["title"]) == ["Miami: Protest at county hall over flooding"]          # Paris is outside the area
    assert items["url"][0].endswith("protest-at-county-hall-over-flooding.html")
    assert items["details"][0].startswith("protest · 12 mentions · Miami, Florida") and items["magnitude"][0] == 12
    asked.clear()
    feed.fetch(client)
    assert asked == ["lastupdate.txt"]                         # files already read aren't fetched again


def test_shared_downloads_one_request_per_url():
    calls = []
    shared = SharedDownloads(client_serving(b"{}", calls))
    for _ in range(3):
        shared.get("https://nhc.example/CurrentStorms.json")
    shared.get("https://nhc.example/other.json")
    shared.get("https://nhc.example/CurrentStorms.json", auth=("id", "pwd"))   # logins are never shared
    assert calls.count("https://nhc.example/CurrentStorms.json") == 2
    assert calls.count("https://nhc.example/other.json") == 1
