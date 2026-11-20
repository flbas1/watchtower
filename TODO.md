# TODO

Claude writes the code and keeps this file, README.md and CONFIG.md up to date; the owner steers.

## Done (2026-11-18)
- [x] Merged osint (feed readers, writers, icons) and osint-web (the page) into one headless
      project for a server or container. Qt left out: a plain-Python scheduler, writer timer,
      config loader and web server instead (README "What changed in the copy").
- [x] `uv run watchtower` serves the page on localhost:8765 until Ctrl+C / SIGTERM;
      `--once` fetches everything once and exits.
- [x] The owner's feed catalog (162 feeds) copied from osint's config.yml into watchtower's
      config.yml (not in git); `config.example.yml` has the defaults.
- [x] systemd unit (`deploy/watchtower.service`) and the Proxmox LXC steps (README).
- [x] Tests: osint's reader and writer tests, plus the scheduler, config, server and writer
      timer (31 pass).
- [x] Real run on the work PC: 175 feeds in about 2 minutes (25 failed: sites the work
      network blocks), the page served from localhost with markers, rows, icons and both
      NASA pictures; config.yml not reachable through the server; the network address
      refused (localhost only, as set).
- [x] Page setting **Slide time** (seconds a static headline takes to slide out and the
      next in; was fixed at 0.6 s): the owner found the slide "very very fast" even at 30 s
      "Seconds each", which is how long a headline stays, not the slide.
- [x] Running in the owner's Proxmox LXC (copied as a package, not from GitHub yet): first
      run 175 feeds, 7 failed (Reddit: 429 Too Many Requests, all asked at once).
- [x] Reddit fixes (the owner: "build both"): `pace` (requests to a site one at a time, N
      seconds apart; reddit.com 5 s) and `retry` (a failed feed tries again after 15 min,
      doubling to at most 4 h, instead of stopping until restart). Tests for both; the
      retry test found the scheduler slept past a sooner retry, fixed.
- [x] Map fixes (the owner): **one Earth** when the world fits across the map (centred, no
      repeats, no sideways drag; zoomed in it still wraps); **rain hides** past a zoom per
      map (satellite 4, radar 8: "Rain up to zoom" in the map popup); **colours per layer**
      (quakes yellow, storms red, storm reports blue, alerts orange, news by place purple,
      airports green, outages pink, TFRs light red, satellites cyan, GPS jamming magenta;
      planes white / military olive / flagged gold / emergency red), each feed's colour
      changeable per map in its popup (a swatch by each feed, which doubles as a legend).
      Flights now carry their `flags` in geo.js.
- [x] Bug (the owner, 2026-11-19): **rain couldn't be removed**. "Add map" put every feed in the
      new map, the rain ones too, and the popup hides those (they belong to the Rain switch),
      so the satellite rain and clouds drew even with Rain off. New maps leave them out, and
      every map ignores them in its feed list, so maps made before the fix work too.
- [x] **One way to turn rain on and off** (the owner: "this should be 1"). There were two: the
      Rain switch and the "Rain radar (Florida)" tick. The switch is gone; Rain radar (world),
      Rainfall (satellite) and Storm clouds (satellite) are ticks in the feed list like
      Florida's, still drawn together at the map's "Rain opacity". Maps saved with the old
      switch start with all three unticked.
- [x] Map popup: ticking a feed no longer scrolls the feed list back to the top.
- [x] **Sharp maps zoomed in** (the owner chose NASA GIBS): past about zoom 3-4, where the
      page's own 5400 px pictures blur, it draws NASA GIBS tiles of the same Blue Marble
      (day) and Black Marble 2016 (night) over them, sharp to about zoom 7 (500 m a pixel;
      no roads). Night tiles load only where it's dark. Loaded by the browser straight from
      gibs.earthdata.nasa.gov, free, no key.
- [x] **Fires** (the owner: both): `fire_hotspots` (NASA FIRMS, every satellite heat spot in the
      last 24 h worldwide at 80%+ confidence, about 3,000; small orange dots) and
      `us_wildfires` (NIFC, about 200 named US fires not yet out, with acres and % contained;
      orange-red). Ticked per map like any feed; not on the marquees (a feed's `marquee:
      true` would put them there).
- [x] **Streets** (the owner: OpenStreetMap over NASA): a map's "Streets" setting is how
      opaque OpenStreetMap is over the picture (0 = off, the default). Maps now zoom to 14
      (was 8; the owner chose 14) so street names can be read. Tiles come from
      tile.openstreetmap.org, credited on the map, only for the view.
      Then (the owner): only **past a zoom** ("past zoom", per map, default 5); up to it, just the
      Blue Marble.

- [x] **Ships, floods, traffic** (the owner): `ships_world` (aisstream.io AIS, worldwide by
      the owner's choice, live websocket kept open, ships not heard from in 30 min dropped; needs
      the owner's free aisstream.io key), `flood_alerts_us` (every NWS flood / flash flood /
      coastal flood warning, watch and advisory) and `river_gauges_flooding` (NOAA gauges at
      minor / moderate / major), `fhp_incidents_south_florida` (Highway Patrol live
      incidents, Miami-Dade + Broward) and `traffic_fl511_south_florida` (FL511 incidents and
      closures there). PulsePoint (fire / EMS calls) left out: behind a CAPTCHA.
      FL511 hands out no developer keys on its site, so (the owner: "switch it") the feed reads
      fl511.com's own pages instead, no key: the Incidents List's data for the details, the
      map's icon lists for the places. Not a published API: it can change without notice.
- [x] **Map popup easier to use** (the owner): the feed list is **groups inside groups** (Air /
      Flights, Weather / Rain, Weather / Floods, Traffic, Fire...; a feed's `group:` in
      config.yml overrides its format's default), each folding open and shut, with a tick that
      turns the whole group on or off. **Track callsigns / tails** boxes in the popup draw
      those aircraft on that map without touching config.yml (kept in the browser; adsb.fi
      refuses web pages, so watchtower's server relays the lookups, read-only, nothing saved).

## Next
- [ ] Ships not yet seen live: waiting on the owner's aisstream.io key.
- [ ] Create the GitHub repo and push (waiting on the owner: private or public).
- [ ] Alerts (osint's quake / keyword alert windows) aren't here: a page-side version?
- [ ] The page's own settings are per browser; a layout saved on the server would follow
      you between browsers.
- [ ] Not tested on Linux yet (only Windows); the container run will be the first.
