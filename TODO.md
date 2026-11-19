# TODO

Claude writes the code and keeps this file, README.md and CONFIG.md up to date; Chris steers.

## Done (2026-11-18)
- [x] Merged osint (feed readers, writers, icons) and osint-web (the page) into one headless
      project for a server or container. Qt left out: a plain-Python scheduler, writer timer,
      config loader and web server instead (README "What changed in the copy").
- [x] `uv run watchtower` serves the page on localhost:8765 until Ctrl+C / SIGTERM;
      `--once` fetches everything once and exits.
- [x] Chris's feed catalog (162 feeds) copied from osint's config.yml into watchtower's
      config.yml (not in git); `config.example.yml` has the defaults.
- [x] systemd unit (`deploy/watchtower.service`) and the Proxmox LXC steps (README).
- [x] Tests: osint's reader and writer tests, plus the scheduler, config, server and writer
      timer (31 pass).
- [x] Real run on the work PC: 175 feeds in about 2 minutes (25 failed: sites the work
      network blocks), the page served from localhost with markers, rows, icons and both
      NASA pictures; config.yml not reachable through the server; the network address
      refused (localhost only, as set).
- [x] Page setting **Slide time** (seconds a static headline takes to slide out and the
      next in; was fixed at 0.6 s): Chris found the slide "very very fast" even at 30 s
      "Seconds each", which is how long a headline stays, not the slide.
- [x] Running in Chris's Proxmox LXC (copied as a package, not from GitHub yet): first
      run 175 feeds, 7 failed (Reddit: 429 Too Many Requests, all asked at once).
- [x] Reddit fixes (Chris: "build both"): `pace` (requests to a site one at a time, N
      seconds apart; reddit.com 5 s) and `retry` (a failed feed tries again after 15 min,
      doubling to at most 4 h, instead of stopping until restart). Tests for both; the
      retry test found the scheduler slept past a sooner retry, fixed.
- [x] Map fixes (Chris): **one Earth** when the world fits across the map (centred, no
      repeats, no sideways drag; zoomed in it still wraps); **rain hides** past a zoom per
      map (satellite 4, radar 8: "Rain up to zoom" in the map popup); **colours per layer**
      (quakes yellow, storms red, storm reports blue, alerts orange, news by place purple,
      airports green, outages pink, TFRs light red, satellites cyan, GPS jamming magenta;
      planes white / military olive / flagged gold / emergency red), each feed's colour
      changeable per map in its popup (a swatch by each feed, which doubles as a legend).
      Flights now carry their `flags` in geo.js.

## Next
- [ ] Create the GitHub repo and push (waiting on Chris: private or public).
- [ ] Alerts (osint's quake / keyword alert windows) aren't here: a page-side version?
- [ ] The page's own settings are per browser; a layout saved on the server would follow
      you between browsers.
- [ ] Not tested on Linux yet (only Windows); the container run will be the first.
