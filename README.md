# ed2k-indexer

A self-hosted Torznab-compatible indexer that bridges **Radarr/Sonarr** with **eMule** as a download client. Allows you to use your existing eMule setup as an automatic download backend for your media stack.

## How it works

```
Jellyseerr → Radarr / Sonarr
                  ↓
         ed2k-indexer (Torznab)
                  ↓
         ed2k link extraction
                  ↓
         eMule WebInterface
                  ↓
         eMule downloads the file
                  ↓
         Radarr/Sonarr imports → Jellyfin
```

1. Radarr or Sonarr sends a search request to the indexer via Torznab
2. The indexer scrapes the configured source, extracts ed2k links, and returns them as a fake torrent feed
3. When Radarr/Sonarr grabs a result, the indexer sends the ed2k link directly to eMule via its WebInterface
4. Once eMule finishes downloading, Radarr/Sonarr detects the file in the Incoming folder and imports it

Quality filtering is handled by Radarr/Sonarr's Quality Profiles — the indexer passes all available sources so your *arr app can pick the best match.

---

## Stack

| Service | Role |
|---|---|
| Radarr | Movie management & import |
| Sonarr | Series management & import |
| ed2k-indexer | Torznab server (this project) |
| eMule | ed2k download client |
| Jellyfin | Media server |
| Jellyseerr | Request frontend (optional) |

---

## Requirements

- Docker + Docker Compose
- eMule running on the host with WebInterface enabled (port 4711)
- A TMDB API key (free): https://www.themoviedb.org/settings/api
- An OMDb API key (free): https://www.omdbapi.com/apikey.aspx

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/tronarite/ed2k-indexer
cd ed2k-indexer
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
INDEXER_USER=tu_usuario
INDEXER_PASS=tu_password
INDEXER_BASE_URL=https://tu-sitio.org
EMULE_PASS=tu_password_emule
TMDB_API_KEY=tu_tmdb_api_key
OMDB_API_KEY=tu_omdb_api_key
TORZNAB_APIKEY=elige_una_clave
```

### 3. Configure volume paths

Edit `docker-compose.yml` and set your actual paths:

```yaml
- /ruta/descargas/Incoming:/downloads   # eMule's Incoming folder
- /ruta/descargas/watch:/watch          # ed2k watch folder
- /ruta/media/movies:/movies            # Radarr root folder
- /ruta/media/series:/series            # Sonarr root folder
```

### 4. Enable eMule WebInterface

In eMule: `Preferences → Web Interface` → enable it on port **4711** and set a password.

### 5. Start

```bash
docker compose up -d
```

---

## Radarr / Sonarr configuration

### Add the indexer

Go to `Settings → Indexers → Add → Torznab`:

| Field | Value |
|---|---|
| URL | `http://ed2k-indexer:8085` |
| API Key | value from `TORZNAB_APIKEY` in your `.env` |
| Categories | `2000` (Movies) / `5000` (TV) |

### Add the download client

Go to `Settings → Download Clients → Add → Torrent Blackhole`:

| Field | Value |
|---|---|
| Torrent Folder | your `/downloads` mapped path |
| Watch Folder | same as above |

---

## Project structure

```
ed2k-indexer/
├── docker-compose.yml
├── .env.example
├── emule_restart.py        ← optional: restarts eMule every 90 min (run on host)
└── ed2k-indexer/
    ├── torznab_server.py   ← main Flask server
    ├── scraper.py          ← movie scraper
    └── scraper_tv.py       ← TV series scraper
```

---

## Optional: eMule auto-restart

If eMule tends to stall, run `emule_restart.py` on the **host** (not in Docker). It kills and relaunches eMule every 90 minutes:

```bash
python emule_restart.py
```

To run it automatically on Windows startup, register it as a scheduled task:

```powershell
$action  = New-ScheduledTaskAction -Execute "python" -Argument "C:\path\to\emule_restart.py"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "eMule Restart" -Action $action -Trigger $trigger -RunLevel Highest
```

---

## Notes

- The indexer uses TMDB to resolve titles and translate them to the local language before searching, improving match rates for non-English titles
- When an exact title search fails, the indexer falls back to a short keyword search and verifies each result by IMDB ID — this handles cases where the source indexes films under a different title variant
- Quality selection is fully delegated to Radarr/Sonarr — configure your Quality Profiles there

---

## License

MIT
