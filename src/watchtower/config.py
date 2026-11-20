"""Watchtower's settings: config.yml next to the code (or the path given with --config).

Everything has a default, so a missing config.yml, or one that only lists what's different,
works. The defaults below are also what config.example.yml shows.
"""

from copy import deepcopy
from pathlib import Path

from ruamel.yaml import YAML, YAMLError

PROJECT = Path(__file__).resolve().parents[2]          # src/watchtower/config.py -> the project folder

DEFAULTS: dict[str, object] = {
    "location": {"lat": 25.76, "lon": -80.19},          # where "Location" centres the page's maps
    # A story whose title contains any of these (any case) is dropped from every feed.
    "skip_keywords": [],
    # The web page's folder: index.html and images are in it, and news.js, geo.js, icons/ and
    # layers/ are written to it. Relative paths are relative to the project folder.
    "web": {"folder": "web"},
    # The small web server for the page. 127.0.0.1: only this machine (or an SSH tunnel /
    # reverse proxy) can open it; 0.0.0.0 would open it to the whole network.
    "server": {"host": "127.0.0.1", "port": 8765},
    # Sites that limit how fast you ask: their requests go one at a time, this many seconds
    # apart (Reddit answers "429 Too Many Requests" when several feeds ask at once).
    "pace": {"www.reddit.com": 5},
    # A feed that fails tries again after first_minutes, then twice as long each time it
    # fails again, at most max_minutes apart.
    "retry": {"first_minutes": 15, "max_minutes": 240},
    # Each feed: an English name, where to get it (url; none for feeds that build their own
    # requests), which FeedProcessor reads it (format), and which AuthType signs the request
    # (authtype). A new RSS news source needs only an entry here with format: rss, and no code.
    "feed_catalog": {
        "hurricane": {"name": "NHC storms", "url": "https://www.nhc.noaa.gov/CurrentStorms.json",
                      "format": "nhc_storms", "update_minutes": 240},        # where storms are now
        "hurricane_track": {"name": "NHC past tracks", "url": "https://www.nhc.noaa.gov/CurrentStorms.json",
                            "format": "nhc_track", "update_minutes": 240},   # where they have been
        "hurricane_cone": {"name": "NHC forecast cones", "url": "https://www.nhc.noaa.gov/CurrentStorms.json",
                           "format": "nhc_cone", "update_minutes": 240},     # cone + forecast points
        "bbc": {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
                "format": "rss", "check": "etag", "update_minutes": 5},     # name is the marquee badge
        "sun": {"name": "Sun", "format": "sun", "update_minutes": 4},        # no url: calculated; moves ~1° in 4 minutes
        # USGS M4.5+ earthquakes of the past day. OPTIONAL: listed so it's ready to use, but
        # nothing shows or watches it until a view names it or you give it an `alert:` block
        # (e.g. alert: {min_magnitude: 6, near_km: 500}; see CONFIG.md).
        "earthquakes": {"name": "USGS", "update_minutes": 10, "format": "usgs_quakes",
                        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"},
        # Every active National Weather Service alert from the Miami office (South Florida):
        # warnings, watches, advisories, statements, each with its area. `sender` keeps one
        # office's alerts; leave it out for the whole state in the url.
        "nws_alerts_miami": {"name": "NWS alerts (Miami)", "update_minutes": 5, "format": "nws_alerts",
                             "url": "https://api.weather.gov/alerts/active?area=FL", "sender": "NWS Miami FL"},
        # What was seen in the last `hours` in the Miami office's area (flooding, hail, wind
        # damage...): NWS Local Storm Reports via Iowa State's mesonet. No url: the reader
        # builds it from `office` (an NWS office code) and `hours`.
        "nws_storm_reports_miami": {"name": "NWS storm reports (Miami)", "update_minutes": 10,
                                    "format": "iem_storm_reports", "office": "MFL", "hours": 24},
        # Flights, live from adsb.fi (format adsb_fi, no url). The settings say what to ask for:
        # near {lat, lon, nm} (at most 250), military, squawks, callsigns, registrations, and an
        # optional area box [south, west, north, east] to keep. Copy one to make your own.
        "flights_nearby": {"name": "Flights near Fort Lauderdale", "update_minutes": 5, "format": "adsb_fi",
                           "near": {"lat": 26.12, "lon": -80.15, "nm": 25}},
        "flights_military_us": {"name": "Military flights (US)", "update_minutes": 5, "format": "adsb_fi",
                                "military": True, "area": [24, -125, 50, -66]},      # the lower 48
        "flights_emergency": {"name": "Flights in an emergency", "update_minutes": 5, "format": "adsb_fi",
                              "squawks": [7500, 7600, 7700]},                         # hijack, radio failure, emergency
        "flights_watch": {"name": "Flights I'm watching", "update_minutes": 5, "format": "adsb_fi",
                          "callsigns": [], "registrations": []},                     # e.g. callsigns: [JBU2926]
        # Where it's raining now: the US NEXRAD composite (5-minute updates), cut to `area`
        # [south, west, north, east]. The lower 48 only.
        "rain_radar_florida": {"name": "Rain radar (Florida)", "update_minutes": 5, "format": "iem_radar",
                               "url": "https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/n0r_0.png",
                               "area": [24.0, -88.0, 31.5, -79.0]},
        # Where it's raining worldwide (wherever there's ground radar): RainViewer's mosaic. Ticked
        # on a map like any feed.
        "rain_radar_world": {"name": "Rain radar (world)", "update_minutes": 5, "format": "rainviewer",
                             "url": "https://api.rainviewer.com/public/weather-maps.json"},
        # Flights with adsb.fi's flags (government and other notable aircraft, owners who hide
        # their identity or limit tracking) within 250 nm. Any flights feed can use `flags`.
        "flights_interesting": {"name": "Interesting aircraft (South Florida)", "update_minutes": 5, "format": "adsb_fi",
                                "near": {"lat": 26.12, "lon": -80.15, "nm": 250},
                                "flags": ["interesting", "pia", "ladd"], "marquee": True},
        # FAA temporary flight restrictions (VIP movements, security, fires, launches), US-wide.
        "flight_restrictions": {"name": "Flight restrictions (FAA TFRs)", "update_minutes": 15, "format": "faa_tfr",
                                "url": "https://tfr.faa.gov/tfrapi/exportTfrList"},
        # FAA ground stops, ground delays, arrival/departure delays and closure notices.
        "airport_status": {"name": "Airport status (FAA)", "update_minutes": 5, "format": "faa_airport_status",
                           "url": "https://nasstatus.faa.gov/api/airport-status-information"},
        # Satellites: the space stations and the ~150 brightest, where they are now and when
        # they pass visibly over `observer` in the next `hours`.
        "satellites": {"name": "Satellites", "update_minutes": 10, "format": "celestrak_satellites",
                       "groups": ["stations", "visual"], "observer": {"lat": 26.12, "lon": -80.15}, "hours": 12},
        # Where aircraft reported jammed or spoofed GPS (gpsjam.org, daily), worldwide.
        "gps_jamming": {"name": "GPS jamming", "update_minutes": 360, "format": "gpsjam",
                        "threshold": 0.1, "min_aircraft": 3},
        # Internet outages by country over the last day (Georgia Tech's IODA).
        "internet_outages": {"name": "Internet outages", "update_minutes": 30, "format": "ioda_outages", "hours": 24},
        # Fires: heat spots satellites saw in the last 24 h, worldwide (NASA FIRMS, MODIS; also
        # flares and farm burns), and US wildfires not yet declared out, with acres (NIFC).
        "fire_hotspots": {"name": "Fire hotspots (satellite)", "update_minutes": 60, "format": "firms_hotspots",
                          "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv",
                          "min_confidence": 80},
        "us_wildfires": {"name": "US wildfires", "update_minutes": 30, "format": "nifc_wildfires",
                         "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"
                                "?where=IncidentTypeCategory%3D%27WF%27+AND+FireOutDateTime+IS+NULL&outFields=*&outSR=4326&f=json"},
        # Floods, US-wide: every NWS flood, flash flood and coastal flood warning, watch and
        # advisory with its area, and NOAA river gauges at minor, moderate or major flood stage.
        "flood_alerts_us": {"name": "Flood alerts (US)", "update_minutes": 10, "format": "nws_alerts", "group": "Weather / Floods",
                            "url": "https://api.weather.gov/alerts/active?event=Flash%20Flood%20Warning,Flood%20Warning,"
                                   "Flood%20Advisory,Flash%20Flood%20Watch,Flood%20Watch,Coastal%20Flood%20Warning,"
                                   "Coastal%20Flood%20Watch,Coastal%20Flood%20Advisory"},
        "river_gauges_flooding": {"name": "River gauges in flood (US)", "update_minutes": 15, "format": "nws_river_gauges",
                                  "url": "https://mapservices.weather.noaa.gov/eventdriven/rest/services/water/riv_gauges/MapServer/0/query"
                                         "?where=status+IN+(%27minor%27,%27moderate%27,%27major%27)&outFields=*&outSR=4326&f=json"},
        # Traffic: Florida Highway Patrol's live incidents (crashes, disabled vehicles, debris,
        # roads under water) in `counties`, and FL511's events of `types` there (read from
        # fl511.com's own pages, no key; also Congestion, Disabled Vehicles, Construction Zones,
        # Road Condition).
        "fhp_incidents_south_florida": {"name": "FHP incidents (Miami-Dade + Broward)", "update_minutes": 5,
                                        "format": "fhp_incidents",
                                        "url": "https://trafficincidents.flhsmv.gov/SmartWebClient/CadView.aspx",
                                        "counties": ["MIAMI-DADE", "BROWARD"]},
        "traffic_fl511_south_florida": {"name": "FL511 traffic (Miami-Dade + Broward)", "update_minutes": 5,
                                        "format": "fl511_events",
                                        "types": ["Incidents", "Closures"], "counties": ["Miami-Dade", "Broward"]},
        # Ships: AIS positions live from aisstream.io, worldwide (strongest along coasts and in
        # ports, where volunteers' receivers are). Needs a free API key from aisstream.io in
        # `key`; `area` [south, west, north, east] narrows it.
        "ships_world": {"name": "Ships (AIS)", "update_minutes": 2, "format": "aisstream", "key": None,
                        "max_age_minutes": 30},
        # Vulnerabilities attackers are exploiting now (CISA), the newest 20.
        "exploited_vulnerabilities": {"name": "Exploited vulnerabilities (CISA)", "update_minutes": 360,
                                      "format": "cisa_kev", "newest": 20,
                                      "url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"},
        # Florida Division of Emergency Management announcements.
        "florida_emergency_management": {"name": "Florida Emergency Management", "update_minutes": 30, "format": "rss",
                                         "url": "https://public.govdelivery.com/accounts/FLDEM/feed.rss"},
        # Filling the rain layer where there's no radar (ticked on a map like any feed, drawn under
        # RainViewer; `rain_layer` is the order, lowest at the bottom). From NASA GIBS:
        # storm clouds from the GOES and Himawari satellites (live; the Americas, Pacific, East
        # Asia, Australia; grey made see-through), and satellite rainfall (global, ~6 h behind).
        "satellite_clouds": {"name": "Storm clouds (satellite)", "update_minutes": 30, "format": "gibs_wms",
                             "layers": ["GOES-West_ABI_Band13_Clean_Infrared", "GOES-East_ABI_Band13_Clean_Infrared",
                                        "Himawari_AHI_Band13_Clean_Infrared"],
                             "drop_grey": True, "rain_layer": 1},
        "satellite_rain": {"name": "Rainfall (satellite)", "update_minutes": 30, "format": "gibs_wms",
                           "layers": ["IMERG_Precipitation_Rate_30min"], "rain_layer": 2,
                           "step_minutes": 30},           # its newest frames are announced before they're filled: find one with rain
        # News by place: GDELT's world news events (every 15 minutes) located inside `area`,
        # kept for `hours`.
        "news_by_place_florida": {"name": "News by place (Florida)", "update_minutes": 15, "format": "gdelt_events",
                                  "area": [24.0, -88.0, 31.5, -79.0], "hours": 6},
    },
}

# Where a feed sits in a map's feed list: groups inside groups, separated by " / ". A feed's own
# `group:` wins; otherwise its format's group here; otherwise "Other".
FORMAT_GROUPS: dict[str, str] = {
    "adsb_fi": "Air / Flights", "faa_tfr": "Air", "faa_airport_status": "Air", "gpsjam": "Air",
    "iem_radar": "Weather / Rain", "rainviewer": "Weather / Rain", "gibs_wms": "Weather / Rain",
    "nhc_storms": "Weather / Hurricanes", "nhc_track": "Weather / Hurricanes", "nhc_cone": "Weather / Hurricanes",
    "nws_alerts": "Weather / Alerts and reports", "iem_storm_reports": "Weather / Alerts and reports",
    "nws_river_gauges": "Weather / Floods",
    "firms_hotspots": "Fire", "nifc_wildfires": "Fire", "usgs_quakes": "Earthquakes",
    "fhp_incidents": "Traffic", "fl511_events": "Traffic", "aisstream": "Ships",
    "celestrak_satellites": "Space", "sun": "Space", "ioda_outages": "Internet", "gdelt_events": "News",
}


def feed_group(feed: dict) -> str:
    return feed.get("group") or FORMAT_GROUPS.get(feed.get("format"), "Other")


# Every feed setting not given takes these.
FEED_DEFAULTS: dict[str, object] = {"name": None, "url": None, "format": None, "authtype": "none",
                                    "update_minutes": 60, "check": None}


def merge(defaults: dict, given: dict) -> dict:
    """`defaults` with `given` laid over it, nested dictionaries merged key by key."""
    out = deepcopy(defaults)
    for key, value in (given or {}).items():
        out[key] = merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def load_config(path: Path | None = None) -> dict:
    """config.yml (or `path`) laid over DEFAULTS. A feed in config.yml replaces nothing else:
    the default catalog's feeds stay unless config.yml sets them to null."""
    path = Path(path) if path else PROJECT / "config.yml"
    given = {}
    if path.exists():
        try:
            given = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        except YAMLError as ex:
            raise SystemExit(f"{path} isn't valid YAML: {ex}")
    cfg = merge(DEFAULTS, given)
    catalog = {}
    for name, feed in cfg["feed_catalog"].items():
        if feed is None:                                  # `name: null` turns a default feed off
            continue
        catalog[name] = merge(FEED_DEFAULTS, feed)
        catalog[name]["name"] = catalog[name]["name"] or name
    cfg["feed_catalog"] = catalog
    folder = Path(cfg["web"]["folder"])
    cfg["web"]["folder"] = folder if folder.is_absolute() else PROJECT / folder
    return cfg
