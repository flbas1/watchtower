# config.yml guide

watchtower reads **`config.yml` in the project folder** (or the file given with `--config`).
It isn't in git, because feeds can hold passwords; `config.example.yml` (in git) shows every
default. Anything left out takes its default, so config.yml only needs what's different.

Changes apply when watchtower restarts (`systemctl restart watchtower` in the container).

Most of this guide is copied from osint's CONFIG.md, the desktop app watchtower came from:
the feed catalog and every `format` work the same. osint's `views`, `wallpaper`, `alerts`
windows and `web: news_file` don't exist here: the web page is the only view.

## YAML in 30 seconds

```yaml
section:              # a group; everything indented under it belongs to it
  key: value          # 2 spaces of indentation, never tabs
  text: BBC           # quotes are optional for plain text...
  url: "https://x"    # ...but harmless; use them if a value contains ': ' or '#'
  empty:              # nothing after the colon = null / None
  flag: true          # true / false
list:                 # a list: each item starts with "- "
- first
- second
inline_list: [a, b]   # the same list on one line
inline_group: {lat: 25.76, lon: -80.19}
```

---

## Top-level settings

### `location`
`{lat, lon}`, default Miami (25.76, -80.19): where a page map's **Location** centre is.

### `skip_keywords`
| Default | Meaning |
|---|---|
| `[]` | a story whose **title contains** any of these, **in any case**, is dropped from every feed: it never reaches a marquee, a map or an alert. Matching is plain text, so `sponsor` also drops "Sponsorship openings…"; write `[sponsor]` to drop only titles with the brackets. Applies after a restart. |

```yaml
skip_keywords: ["[sponsor]", "[ad]"]   # quote entries that start with [ (otherwise YAML reads a list)
```

### `web`
| Key | Default | Meaning |
|---|---|---|
| `folder` | `web` | the page's folder: `index.html` and `images/` are in it, and watchtower writes `news.js`, `geo.js`, `icons/` and `layers/` into it. A relative path is relative to the project folder. |

### `server`
| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | who can open the page. `127.0.0.1`: only this machine (from another one, use an SSH tunnel, see README). `0.0.0.0`: anyone on the network who can reach it. Only the web folder is ever served, never config.yml. |
| `port` | `8765` | the page is at `http://<host>:<port>/`. |

### `pace`
`{site: seconds}`, default `{www.reddit.com: 5}`. Requests to a listed site go one at a
time, at least that many seconds apart, across every feed. For sites that refuse when asked
too fast: Reddit answered "429 Too Many Requests" when its 7 feeds were asked at once.

### `retry`
| Key | Default | Meaning |
|---|---|---|
| `first_minutes` | `15` | a feed that fails tries again this long after. |
| `max_minutes` | `240` | each further failure doubles the wait (15, 30, 60, 120, 240…) up to this. |

Meanwhile the page shows the feed as failing, with why and when it tries again. Once it
works it's back on its own `update_minutes`.

## `feed_catalog`: where data comes from

Each feed is defined **once**, by a name you choose (`bbc:`, `hurricane:` …). Views then list
the names they want. **A feed is only fetched if a view names it** (or it has an `alert:`).

```yaml
feed_catalog:
  bbc:
    name: BBC
    url: https://feeds.bbci.co.uk/news/world/rss.xml
    format: rss
    update_minutes: 5
```

| Key | Default | Meaning |
|---|---|---|
| `name` | the key | English name. RSS uses it as the **source** shown on the marquee badge (keep it short: `BBC`); map-window warnings use it ("1 NHC storms"). |
| `url` | none | where to download from. Leave empty for calculated feeds (`sun`). |
| `format` | **required** | which reader understands the download (see below). |
| `authtype` | `none` | how to log in (see below). |
| `id`, `pwd` | none | user name and password for `authtype: basic`. Plain text for now. |
| `update_minutes` | `60` | how often to fetch. |
| `check` | none | `etag`: send the last ETag, so an unchanged feed costs almost nothing and causes no redraw. Only helps if the site sends ETags (NHC does; BBC doesn't). |
| `alert` | none | rules that pop an **alert window** (see "Alerts" below). A feed with an `alert` is fetched even if no view shows it. |
| `source` | — | left over from an older design; ignored. |

### `format` values
| Value | Reads | Gives |
|---|---|---|
| `rss` | any news feed: RSS 2.0, RSS 1.0 (RDF) or Atom | headlines with links (no place). An unreadable date leaves that item undated instead of failing the feed; relative links are resolved against the feed's website. |
| `sun` | nothing (calculated, no `url`) | where the sun is overhead; drawn as a sun symbol |
| `nhc_storms` | NHC `CurrentStorms.json` | each active storm's position (a marker) |
| `nhc_track` | the same `url` | each storm's past track (a line) |
| `nhc_cone` | the same `url` | each storm's forecast cone and forecast points |
| `usgs_quakes` | a USGS GeoJSON summary, e.g. `…/summary/4.5_day.geojson` | each quake as a marker ("M 5.1 - 80 km W of …"), with magnitude, depth and tsunami flag for alerts |
| `nws_alerts` | National Weather Service active alerts, e.g. `https://api.weather.gov/alerts/active?area=FL` (`area=` a state, or `zone=`, `point=lat,lon`). Optional **`sender`**: keep one office's alerts, e.g. `NWS Miami FL` | every active warning, watch, advisory and statement ("Flash Flood Warning: Broward, FL"), with its area as polygons. Warnings carry their own polygon; watches and advisories only name forecast zones, so osint downloads the zones' outlines (once per zone while it runs). Details: severity, urgency, when it ends. The link is forecast.weather.gov's hazard page for the alert's zone and county, opened on that alert (its full text, readable); for a warning that names only counties, osint looks up a zone in the county once (`/points`). If no zone can be found, the link is the alert's API page (JSON). |
| `iem_storm_reports` | nothing in `url`: NWS Local Storm Reports from Iowa State's mesonet, for **`office`** (an NWS office code, e.g. `MFL` = Miami) over the last **`hours`** (default 24). Optional **`search`**: the search address for the links, with `{query}` where the words go (default `https://www.google.com/search?q={query}`) | each report of what was seen (flooding, hail, wind damage…) as a marker at its spot ("Flash flood: 2 W Hollywood"), with the remark, county and size (hail inches, gust mph) as details. The link is a web search for the remark, county and state, to find the news story or post behind it; a report with no remark links to the NWS report instead. |
| `adsb_fi` | nothing in `url`: live aircraft from adsb.fi's open data (no account). Say what to ask for with any of: **`near`** `{lat, lon, nm}` (everything within `nm` nautical miles, at most 250), **`military`** `true` (worldwide), **`squawks`** `[7500, 7600, 7700]` (worldwide), **`callsigns`** `[JBU2926, …]`, **`registrations`** `[N972QS, …]`; optional **`area`** `[south, west, north, east]` keeps only planes inside that box. The answers are merged, each plane once. adsb.fi asks for at most one request a second, so osint spaces out every flight request. | each plane at its position, pointing its heading ("JBU2926 · A320 · 8,500 ft"), with type, owner/operator, tail number, speed, squawk and "military" as details; red when squawking 7500 (hijack), 7600 (radio failure) or 7700 (emergency). The link is the plane's live track on globe.adsb.fi. |
| `iem_radar` | the US NEXRAD rain radar composite in flat lat/lon, e.g. `https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/n0r_0.png` (about 1 km per pixel; its world file, the same name ending `.wld`, says where it lies). Optional **`area`** `[south, west, north, east]`: the box cut out (the lower 48 only) | one picture: osint saves the cut-out as `layers/<feed>.png` next to `geo.js`, with "no rain" see-through, and `geo.js` says where it goes. The page lays it on the map between the picture and the markers. The finer `n0q_0.png` (0.005°) exists but is too heavy for the browser on this PC. It stops at the edges of its box. |
| `rainviewer` | RainViewer's radar mosaic index, `https://api.rainviewer.com/public/weather-maps.json` (free; the page shows a "Rain: RainViewer" credit as its terms ask) | the newest frame's tile address (every 10 minutes, worldwide wherever there's ground radar; blank over open ocean). Ticked in a map's feed list like any feed; drawn at the map's **Rain opacity**, and the page loads only the tiles in view straight from RainViewer. |
| `gibs_wms` | nothing in `url`: one world picture from NASA GIBS. **`layers`** (drawn together), **`width`** / **`height`** (default 2048 × 1024), **`drop_grey`** (grey made see-through: a colour-enhanced cloud picture shows only its cold storm tops), **`step_minutes`** (for layers whose newest frames are announced before they're filled: osint checks tiny versions, newest first, back to the last one with data or **`look_back_hours`**, default 12), **`rain_layer`** (a number: when ticked on a map, the page draws it under the RainViewer radar at the map's **Rain opacity**, lowest first) | the picture saved as `layers/<feed>.png`. In the catalog: `satellite_clouds` (GOES-West + GOES-East + Himawari infrared, every 10 minutes, grey dropped; covers the Americas, the Pacific, East Asia and Australia, not Europe/Africa) and `satellite_rain` (NASA IMERG rainfall every 30 minutes, worldwide including oceans, about 6-7 hours behind). |
| `gdelt_events` | nothing in `url`: GDELT 2.0's event files (every 15 minutes, the world's news events with where they happened and an article link). **`area`** `[south, west, north, east]`: keep events located inside it; **`hours`** (default 6): how long an event stays; at start osint fetches the earlier files to fill those hours | each article and place as a marker ("Miami: Nashville man arrested after dna evidence links…"), the title made from the article link (or the site's name when the link has no words), with the kind of event, how many articles mention it, the place and the site as details. Also in `news.js`, so a marquee row can show them. |
| `faa_tfr` | the FAA's current flight restrictions, `https://tfr.faa.gov/tfrapi/exportTfrList`; their shapes come from the FAA's map service (which also keeps old ones, so only current NOTAMs are used). Optional `area` | each TFR ("TFR 6/1234 Security: Palm Beach, FL…") with its area, shaded red on the page, and a link to the FAA's page for it. A few (e.g. nationwide "Special" ones) have no shape and only show in marquees. |
| `faa_airport_status` | `https://nasstatus.faa.gov/api/airport-status-information`. Optional `area` | each ground stop, ground delay program, arrival/departure delay and closure notice ("SFO: ground stop (wind) until 12:00 pm PST") at its airport, whose position osint looks up once from weather.gov's station list (K + code; Alaska/Hawaii P; Puerto Rico TJ + the first two letters). |
| `cisa_kev` | CISA's Known Exploited Vulnerabilities catalog, `…/known_exploited_vulnerabilities.json` (1.8 MB: update daily). **`newest`** (default 20) | the newest additions ("CVE-2026-…: …"), linking to NVD, with vendor, product, the fix-by date and "used in ransomware". No places: marquees only. |
| `ioda_outages` | nothing in `url`: Georgia Tech's IODA, internet outages by country over the last **`hours`** (default 24); optional **`min_score`** | each country with an outage ("Internet outage: Iran"), worst first, at the country's centre (looked up once per country from OpenStreetMap, one request a second), linking to IODA's page for it. |
| `celestrak_satellites` | nothing in `url`: CelesTrak orbits for **`groups`** (default `[stations, visual]`: the space stations and ~150 brightest), with the `sgp4` library; **`observer`** `{lat, lon}`, **`hours`** (default 12), **`min_elevation`** (default 10) | each satellite where it is now ("ISS (ZARYA) · 418 km · visible 6:42 PM, 54° high" when it will be seen: the observer's sky dark, the satellite sunlit, at least `min_elevation` up). Visible passes first, soonest first. Times in this PC's local time. |
| `gpsjam` | nothing in `url`: gpsjam.org's daily file (usually yesterday's), with the `h3` library. **`threshold`** (default 0.1 = 10% of aircraft with bad GPS), **`min_aircraft`** (default 3), optional `area` | each hexagon cell over the threshold ("GPS interference 45% (9 of 20 aircraft)"), shaded magenta on the page, worst first, linking to gpsjam.org there. About 800 cells worldwide at 10%. |
| `firms_hotspots` | a NASA FIRMS 24-hour CSV, e.g. `https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv` (public, no key). **`min_confidence`** (MODIS's 0-100, default 80: about 3,000 spots worldwide; 0 keeps all, about 11,000), optional `area` | each heat spot satellites saw in the last 24 hours ("Fire hotspot: 37 MW, 98% confidence": wildfires, also gas flares and farm burns), small orange dots on the page, linking to the FIRMS map there. |
| `nifc_wildfires` | NIFC's current wildfire incidents (WFIGS) query, e.g. the `us_wildfires` url in config.example.yml. Optional `area` | each US wildfire not yet declared out ("Aspen Acres fire: 102,007 acres, 96% contained"), at its point of origin, with county, state, cause and when found. About 200 at a time. |

Any feed can also take **`marquee: true / false`**: whether it goes into `news.js` for the page's marquee rows. By default news, weather alerts, storm reports, news by place, TFRs, airport status, vulnerabilities, outages, satellites and GPS jamming do; flights don't (`flights_interesting` sets `marquee: true`).

`adsb_fi` feeds can take **`flags`**: `[military, interesting, pia, ladd]` keeps only planes adsb.fi flags so (interesting = on its list of notable aircraft, often government; pia / ladd = owners who asked to hide their identity or limit tracking).

An unknown `format` skips that feed with a message; the rest of the app runs.

Turning a default feed off: set it to `null`, e.g. `bbc: null` under `feed_catalog`.

### `authtype` values
| Value | Needs |
|---|---|
| `none` | nothing |
| `basic` | `id` and `pwd` |

### Adding a news source
No code needed: add an entry with `format: rss`, then name it in a view's `feeds:`.

```yaml
feed_catalog:
  guardian:
    name: Guardian
    url: https://www.theguardian.com/world/rss
    format: rss
    update_minutes: 10
```

---
