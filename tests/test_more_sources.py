"""The OSINT sources added 2026-11-18: flight restrictions, airport status, exploited
vulnerabilities, internet outages, satellites, GPS jamming, interesting aircraft, and which
feeds go to the marquees and maps."""
from datetime import datetime, timedelta, timezone

import h3
import httpx
import pandas as pd

from watchtower import feeds
from watchtower.feeds import Feed
from watchtower.web_news import geo_feeds, web_feeds


def client_for(routes):
    """routes: [(text in the url, response json or text or callable)]"""
    def handler(request):
        url = str(request.url)
        for part, answer in routes:
            if part in url:
                if callable(answer):
                    return answer(request)
                return httpx.Response(200, **({"text": answer} if isinstance(answer, str) else {"json": answer}))
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tfrs_only_current_ones_with_their_shapes():
    current = [{"notam_id": "6/1234", "type": "SECURITY", "facility": "ZMA", "state": "FL", "description": "Palm Beach",
                "creation_date": "11/18/2026"}]
    square = {"type": "Polygon", "coordinates": [[[-80.1, 26.6], [-80.0, 26.6], [-80.0, 26.7], [-80.1, 26.6]]]}
    shapes = {"features": [{"properties": {"NOTAM_KEY": "6/1234-1-FDC-F", "TITLE": "Palm Beach, FL, VIP"}, "geometry": square},
                           {"properties": {"NOTAM_KEY": "5/6914-1-FDC-F", "TITLE": "an old one"}, "geometry": square}]}
    client = client_for([("exportTfrList", current), ("geoserver", shapes)])
    items = Feed("tfr", {"name": "TFRs", "format": "faa_tfr", "url": "https://tfr.faa.gov/tfrapi/exportTfrList"}).fetch(client)
    assert list(items["title"]) == ["TFR 6/1234 Security: Palm Beach, FL, VIP"]          # the stale shape is left out
    assert items["url"][0] == "https://tfr.faa.gov/tfr3/?page=detail_6_1234"
    assert items["geometry"][0][0][0] == "polygon" and round(items["lat"][0], 1) == 26.6


AIRPORTS = """<AIRPORT_STATUS_INFORMATION><Update_Time>Wed Nov 18 18:28:18 2026 GMT</Update_Time>
<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List><Program><ARPT>SFO</ARPT><Reason>wind</Reason>
<End_Time>12:00 pm PST</End_Time></Program></Ground_Stop_List></Delay_type>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List><Delay><ARPT>SJU</ARPT>
<Reason>WX:Thunderstorms</Reason><Arrival_Departure Type="Departure"><Min>16 minutes</Min><Max>30 minutes</Max>
<Trend>Increasing</Trend></Arrival_Departure></Delay></Arrival_Departure_Delay_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""


def test_airport_status_events_placed_at_their_airports(monkeypatch):
    monkeypatch.setattr(feeds.FaaAirportStatusProcessor, "_airports", {})
    asked = []
    def station(request):
        code = str(request.url).rsplit("/", 1)[1]
        asked.append(code)
        spots = {"KSFO": [-122.37, 37.62], "TJSJ": [-66.0, 18.45]}
        return httpx.Response(200, json={"geometry": {"coordinates": spots[code]}}) if code in spots else httpx.Response(404)
    client = client_for([("nasstatus", AIRPORTS), ("/stations/", station)])
    items = Feed("ap", {"name": "Airports", "format": "faa_airport_status",
                        "url": "https://nasstatus.faa.gov/api/airport-status-information"}).fetch(client)
    assert list(items["title"]) == ["SFO: ground stop (wind) until 12:00 pm PST",
                                    "SJU: departure delays 16 minutes to 30 minutes, increasing (WX:Thunderstorms)"]
    assert (items["lat"][1], items["lon"][1]) == (18.45, -66.0)                       # San Juan: KSJU, PSJU, then TJSJ
    assert asked == ["KSFO", "KSJU", "PSJU", "TJSJ"]


def test_exploited_vulnerabilities_newest_first():
    kev = {"vulnerabilities": [
        {"cveID": "CVE-2026-1", "vulnerabilityName": "Old bug", "vendorProject": "A", "product": "B", "dateAdded": "2026-11-01", "dueDate": "2026-11-22"},
        {"cveID": "CVE-2026-2", "vulnerabilityName": "New bug", "vendorProject": "C", "product": "D", "dateAdded": "2026-11-17",
         "dueDate": "2026-12-08", "knownRansomwareCampaignUse": "Known"}]}
    items = Feed("kev", {"name": "KEV", "format": "cisa_kev", "url": "https://cisa.example/kev.json", "newest": 1}).fetch(client_for([("kev", kev)]))
    assert list(items["title"]) == ["CVE-2026-2: New bug"]
    assert items["url"][0] == "https://nvd.nist.gov/vuln/detail/CVE-2026-2"
    assert items["details"][0] == "C D · fix by 2026-12-08 · used in ransomware" and pd.isna(items["lat"][0])


def test_internet_outages_by_country_worst_first(monkeypatch):
    monkeypatch.setattr(feeds.IodaOutagesProcessor, "_places", {})
    monkeypatch.setattr("watchtower.feeds.time.sleep", lambda s: None)
    summary = {"data": [{"scores": {"overall": 100.0}, "event_cnt": 1, "entity": {"code": "XA", "name": "Smallland"}},
                        {"scores": {"overall": 5000.0}, "event_cnt": 3, "entity": {"code": "XB", "name": "Bigland"}}]}
    def place(request):
        return httpx.Response(200, json=[{"lat": "10.0" if request.url.params["country"] == "XA" else "20.0", "lon": "30.0"}])
    client = client_for([("ioda", summary), ("nominatim", place)])
    items = Feed("io", {"name": "Outages", "format": "ioda_outages"}).fetch(client)
    assert list(items["title"]) == ["Internet outage: Bigland", "Internet outage: Smallland"]
    assert (items["lat"][0], items["lon"][0]) == (20.0, 30.0)
    assert items["url"][0] == "https://ioda.inetintel.cc.gatech.edu/country/XB" and items["magnitude"][0] == 5000.0


def test_gps_jamming_cells_over_the_threshold(monkeypatch):
    jammed = h3.latlng_to_cell(54.7, 20.5, 4)                       # near Kaliningrad
    fine = h3.latlng_to_cell(26.1, -80.1, 4)
    few = h3.latlng_to_cell(40.0, -100.0, 4)
    csv = f"hex,count_good_aircraft,count_bad_aircraft\n{jammed},11,9\n{fine},50,1\n{few},1,1\n"
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    client = client_for([(f"{yesterday}-h3_4.csv", csv)])            # today's file isn't out yet: 404
    items = Feed("gps", {"name": "GPS jamming", "format": "gpsjam"}).fetch(client)
    assert list(items["title"]) == ["GPS interference 45% (9 of 20 aircraft)"]        # 2% and 1-of-2 left out
    assert len(items["geometry"][0][0][1]) == 6 and round(items["lat"][0]) in (54, 55)
    assert yesterday in items["details"][0]


ISS_OMM = {"OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": None, "MEAN_MOTION": 15.5,
           "ECCENTRICITY": 0.0005, "INCLINATION": 51.64, "RA_OF_ASC_NODE": 100.0, "ARG_OF_PERICENTER": 50.0,
           "MEAN_ANOMALY": 300.0, "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": "U", "NORAD_CAT_ID": 25544,
           "ELEMENT_SET_NO": 999, "REV_AT_EPOCH": 50000, "BSTAR": 0.0002, "MEAN_MOTION_DOT": 0.0001, "MEAN_MOTION_DDOT": 0}


def test_satellites_where_they_are_now():
    omm = dict(ISS_OMM, EPOCH=(datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.%f"))
    client = client_for([("GROUP=stations", [omm]), ("GROUP=visual", [omm])])   # in both groups: listed once
    items = Feed("sats", {"name": "Satellites", "format": "celestrak_satellites", "hours": 2}).fetch(client)
    assert len(items) == 1 and items["title"][0].startswith("ISS (ZARYA) · ")
    assert -52 <= items["lat"][0] <= 52 and -180 <= items["lon"][0] <= 180            # its orbit's tilt bounds the latitude
    assert 300 < items["magnitude"][0] < 500                                           # height in km
    assert items["url"][0] == "https://www.n2yo.com/satellite/?s=25544"


def test_flights_flags_keep_only_flagged_planes(monkeypatch):
    monkeypatch.setattr("watchtower.feeds.time.sleep", lambda s: None)
    planes = [{"hex": "a1", "flight": "GOV1", "lat": 26.0, "lon": -80.0, "dbFlags": 2},
              {"hex": "a2", "flight": "N1", "lat": 26.1, "lon": -80.1, "dbFlags": 8},
              {"hex": "a3", "flight": "JBU1", "lat": 26.2, "lon": -80.2}]
    client = client_for([("/lat/", {"ac": planes})])
    items = Feed("f", {"name": "F", "format": "adsb_fi", "near": {"lat": 26, "lon": -80, "nm": 250},
                       "flags": ["interesting"]}).fetch(client)
    assert list(items["id"]) == ["a1"] and "interesting" in items["details"][0]


def test_gibs_world_picture_grey_made_see_through():
    import io
    from PIL import Image
    picture = Image.new("RGBA", (4, 2), (128, 128, 128, 255))                # grey: warm ground
    picture.putpixel((1, 0), (255, 0, 0, 255))                                # red: a cold storm top
    png = io.BytesIO(); picture.save(png, "PNG")
    asked = []
    def handler(request):
        asked.append(dict(request.url.params))
        return httpx.Response(200, content=png.getvalue(), headers={"content-type": "image/png"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = Feed("clouds", {"name": "Storm clouds", "format": "gibs_wms", "layers": ["A_Infrared", "B_Infrared"],
                            "drop_grey": True, "width": 4, "height": 2}).fetch(client)
    assert asked[0]["LAYERS"] == "A_Infrared,B_Infrared" and asked[0]["BBOX"] == "-90,-180,90,180"
    out = Image.open(io.BytesIO(items["image"][0]))
    assert out.getpixel((0, 0))[3] == 0 and out.getpixel((1, 0)) == (255, 0, 0, 255)
    assert items["bounds"][0] == [-90.0, -180.0, 90.0, 180.0]

    error = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<ServiceException>no</ServiceException>",
                                                                                headers={"content-type": "text/xml"})))
    try:
        Feed("x", {"name": "X", "format": "gibs_wms", "layers": ["A"]}).fetch(error)
        assert False, "an error report must not pass as a picture"
    except ValueError as ex:
        assert "ServiceException" in str(ex)


def test_marquee_setting_and_feeds_without_places():
    cfg = {"feed_catalog": {"news": {"format": "rss"}, "kev": {"format": "cisa_kev"}, "planes": {"format": "adsb_fi"},
                            "vip": {"format": "adsb_fi", "marquee": True}, "tfr": {"format": "faa_tfr", "marquee": False}}}
    assert web_feeds(cfg) == {"news", "kev", "vip"}
    assert geo_feeds(cfg) == {"planes", "vip", "tfr"}                                  # news and the CVE list have no places
