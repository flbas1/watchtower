# watchtower

Open-source intelligence on one web page: marquees of news and alerts, and maps of storms,
rain, flights, satellites, flight restrictions, GPS jamming, internet outages and more.
Watchtower fetches the feeds in the background and serves the page; you open it in a browser.

It's **headless**: no windows, no tray, no desktop. It runs on any machine with Python,
including a Proxmox LXC container, and you view it from a Mac, a PC or a phone.

Watchtower is the merge (2026-11-18) of two earlier projects:
- **osint** (`C:\work\python\osint`): the Windows tray app. Its feed readers, writers and
  icon finder are copied here as they were; its Qt parts (tray, wallpaper, map and marquee
  windows, Settings window, alert windows) are not, and osint stays the desktop app.
- **osint-web** (`C:\work\python\osint-web`): the web page (`web/index.html`), copied as it
  was, with its images.

What changed in the copy: the feed scheduler, the news writer's timer, the config loader
and the web server are new and plain Python (osint's were built on Qt's timers and signals),
so watchtower needs no Qt.

## Running it

```
uv run watchtower                  # fetch and serve until Ctrl+C
uv run watchtower --once           # fetch every feed once, write the page's files, exit
uv run watchtower --config FILE    # another config file
```

Then open **http://localhost:8765/**. The first fetch of every feed takes about a minute;
the page fills in as files arrive (it reads them again every minute).

- **Stopping:** Ctrl+C in its window, or `systemctl stop watchtower` when it runs as a
  service (below). No tray icon needed on any system.
- **Settings:** `config.yml` next to this file (not in git: feeds can hold passwords).
  `config.example.yml` shows the defaults; CONFIG.md explains every setting. With no
  config.yml at all, the default feed catalog runs.
- **The page's own settings** (rows, maps, speeds) are kept per browser and per address;
  use the page's Export / Import layout to copy them between browsers.

## Where things are

```
src/watchtower/
  __init__.py      the program: config, feeds, writer, icons, web server, Ctrl+C / SIGTERM
  config.py        config.yml over the defaults (the default feed catalog lives here)
  feed_service.py  runs every feed on its own schedule on 4 worker threads; skip keywords
  feeds.py         the feed readers (from osint): rss, NHC, USGS, NWS alerts and storm
                   reports, adsb.fi flights, radar, RainViewer, NASA GIBS, GDELT, FAA TFRs
                   and airport status, CISA KEV, IODA, CelesTrak satellites, gpsjam
  icons.py         finds each news feed's icon (from osint), cached in web/icons
  web_news.py      writes web/news.js, web/geo.js and web/layers/*.png (from osint)
  server.py        serves the web folder, and only it
  sun.py           where the sun is (from osint)
web/
  index.html       the page (from osint-web); images/ the NASA day and night pictures
  news.js, geo.js, icons/, layers/   written by watchtower (not in git)
deploy/
  watchtower.service   systemd unit for a server or container
tests/             pytest: uv run python -m pytest
```

## In a Proxmox LXC container

A small Debian container is plenty: 1 CPU, 1 GB of memory, 4 GB of disk.

1. **Create it** in Proxmox (Debian 12 template), start it, and open its console.
2. **Install the basics:**
   ```
   apt update && apt install -y curl git ca-certificates
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   (uv installs into `~/.local/bin`; log out and back in, or `source ~/.profile`.)
3. **Get watchtower:**
   ```
   git clone https://github.com/bm1-cstewart/watchtower.git /opt/watchtower
   cd /opt/watchtower
   cp config.example.yml config.yml     # then edit: your feeds, location, keywords
   uv run watchtower --once             # a first run: checks every feed, writes the files
   ```
4. **Run it as a service** (starts at boot, restarts if it stops):
   ```
   cp deploy/watchtower.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now watchtower
   journalctl -u watchtower -f          # its output
   ```
5. **Open it from the Mac.** The server only listens on the container itself
   (`server: host: 127.0.0.1`), so reach it through SSH:
   ```
   ssh -L 8765:localhost:8765 root@<container-address>
   ```
   and open http://localhost:8765/ on the Mac while that's connected. To open it to your
   whole network instead, set `server: host: 0.0.0.0` in config.yml (anyone who can reach
   the container can then view the page; only the web folder is served, never config.yml).

To update from GitHub: `cd /opt/watchtower && git pull && systemctl restart watchtower`.

### Updating with the package (until there's a GitHub repo)
Every update is **one file with one name: `watchtower.tar.gz`**: `C:\work\python\watchtower.tar.gz`
on the PC, `/root/watchtower.tar.gz` on the Proxmox host and in the container. It holds
every committed file under `watchtower/` and **never config.yml**, so it can always be
unpacked over an existing install (config.yml and the fetched files stay).

```
# on the PC (PowerShell)
scp C:\work\python\watchtower.tar.gz root@<proxmox-host>:/root/watchtower.tar.gz
# on the Proxmox host
pct push <CTID> /root/watchtower.tar.gz /root/watchtower.tar.gz
pct exec <CTID> -- bash -c "cd /opt && tar xzf /root/watchtower.tar.gz && systemctl restart watchtower"
```

## Not here (yet)

- **Alerts** (osint's pop-up windows for quakes near you, keywords...): they were Qt
  windows. A page-side version is a TODO.
- The desktop wallpaper and the tray dot stay in osint.
