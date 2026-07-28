# unciv-game-service

REST API microservice for managing [Unciv](https://github.com/yairm210/Unciv) multiplayer games.

Handles all direct interaction with Unciv game files and game creation — so the bot layer never touches the filesystem or launches processes directly.

## Features

- **Read game data** — parse save files: current player, turns, player list, capitals, units, cities, diplomacy, techs
- **Map validation** — check start positions: distances between civs, luxury resources, marine civs on coast, edge proximity
- **Async game creation** — launch `Unciv.jar --creategame` with automatic map-check retry loop and task queue (one game at a time)
- **Jar auto-update** — download and replace `Unciv.jar` before game creation (local or SSH mode)
- **Task management** — long-running jobs return a `task_id`; poll for progress and logs; cancel mid-flight; list all tasks
- **Utilities** — patch Great Prophet counters, load spectate backups, compute veto rights

## Architecture

```
Bot / Web Client
      │
      ▼
unciv-game-service  (this repo)
      │
      ├── reads/writes  CIV_PATH/MultiplayerFiles/  (volume mount)
      └── launches Unciv.jar --creategame
            ├── local mode (default): subprocess inside the container
            └── ssh mode: remote host via asyncssh
```

Game files are read directly from the mounted `CIV_PATH/MultiplayerFiles/` directory (the Unciv MP server's data folder).

Unciv saves are `base64(gzip(JSON))` — this service decodes them transparently, all endpoints return plain JSON.

## API

Interactive docs: `/docs` (Swagger UI) · `/redoc`

### Games

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/games` | List all local games with human civs, current player, turn number |
| `GET` | `/games/{id}/info` | Current player, turn, full player list with types |
| `GET` | `/games/{id}/map-check` | Validate map quality: distances, luxuries, marine civs, edge proximity |
| `GET` | `/games/{id}/preview` | Lightweight preview file (current player, turn — fast read) |
| `GET` | `/games/{id}/snapshot` | All tracker data in one parse: techs, units, cities, capitals, diplomacy, veto stats |
| `GET` | `/games/{id}/capitals` | Original capitals for all civs with current owner |
| `GET` | `/games/{id}/units` | All units on the map; filter by `?owner=CivName` |
| `GET` | `/games/{id}/cities` | All cities; filter by `?nation=CivName` |
| `GET` | `/games/{id}/diplomacy` | Diplomatic relations between all civs (war/peace status) |
| `GET` | `/games/{id}/techs` | Researched technologies and adopted policies per civ |
| `GET` | `/games/{id}/veto` | Veto rights per civ (score, tech, culture, force, capitals, Apollo, Utopia) |
| `POST` | `/games/start` | Start async game creation — returns `task_id` immediately |
| `POST` | `/games/{id}/prophet` | Patch Great Prophet counter for a nation |
| `POST` | `/games/{id}/spectate` | Load a `.tar.gz` backup as a spectate game |
| `DELETE` | `/games/{id}` | Delete save file and preview file for a game |

#### `GET /games/{id}/map-check` query params

| Param | Default | Description |
|-------|---------|-------------|
| `min_distance` | from config | Minimum hex distance between start positions |
| `max_distance` | from config | Maximum hex distance between start positions |
| `min_luxuries` | from config | Minimum total luxury resources within radius |
| `min_unique_luxuries` | from config | Minimum unique luxury types within radius |

#### `POST /games/start` body

```json
{
  "config": { ... },         // Unciv --creategame config (see config.example.json)
  "max_attempts": 100,       // override default retry limit (optional)
  "nochecks": false,         // skip map-check and accept any result (optional)
  "noupdate": false,         // skip jar update even if jar_url is provided (optional)
  "jar_url": "https://...",  // zip URL to download Unciv.jar from before starting (optional)
  "mod_git_url": "https://github.com/user/Mod/tree/branch"  // mod repo to clone (SSH mode only, optional)
}
```

Only one game creation task runs at a time. Additional requests are queued in `pending` status and start automatically when the current one finishes.

**Jar update flow** (when `jar_url` is set and `noupdate` is false):

- **Local mode**: downloads the zip, extracts `Unciv.jar` to `UNCIV_JAR_PATH` inside the container
- **SSH mode**: SSHes into the remote host, runs `wget` + `unzip`, optionally clones `mod_git_url` into the work dir

The update runs once per task, before the game-creation retry loop, while holding the task queue lock (so two tasks never update simultaneously).

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks; filter by `?status=pending\|running\|done\|failed\|cancelled` |
| `GET` | `/tasks/{task_id}` | Poll task status, attempt counter, per-attempt log, result or error |
| `POST` | `/tasks/{task_id}/cancel` | Cancel a pending or running task |

#### Task statuses

| Status | Meaning |
|--------|---------|
| `pending` | Queued, waiting for the current running task to finish |
| `running` | Actively updating jar or generating the map |
| `done` | Success — `result.game_id` contains the created game UUID |
| `failed` | All attempts exhausted or fatal error — see `error` field |
| `cancelled` | Cancelled by user |

Completed tasks (done / failed / cancelled) are automatically purged after 1 hour.

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |

## Game creation flow

1. `POST /games/start` with a config — returns `task_id` and `status_url` immediately (HTTP 202)
2. If another task is already running, the new task waits in `pending`
3. When the slot is free, the task becomes `running`:
   - If `jar_url` is set and `noupdate` is false → download and replace `Unciv.jar` first
   - Enter the retry loop:
     - Launch `Unciv.jar --creategame` (local subprocess or SSH)
     - Read the created save and run map-check
     - If map check fails → log the issues and retry with a new random seed
     - If map check passes → task becomes `done` with `result.game_id`
4. Poll `GET /tasks/{task_id}` to follow progress — the `log` array grows with each attempt

## Config format

See [`config.example.json`](config.example.json) for a full example (6-player Pangaea game).

Key fields:

- `gameParameters.players` — array of player objects; include one `Spectator` Human entry
- `gameParameters.multiplayerServerUrl` — your Unciv MP server URL
- `gameParameters.baseRuleset` — mod ruleset name (e.g. `"RekMOD iron"`)
- `mapParameters.type` — map type: `Pangaea`, `Fractal`, `Inner Sea`, `Perlin`, etc.
- `mapParameters.worldWrap` — enable horizontal world wrap
- `mapParameters.mapSize` — `{ "name": "Custom", "radius": N, "width": W, "height": H }`

The `seed` field in `mapParameters` is overwritten automatically on each attempt.

## Configuration

Copy `.env.example` to `.env`:

```env
# Host path to Unciv MultiplayerFiles (mounted into container at /data/unciv)
CIV_PATH=/path/to/unciv/data

# Launcher type: "local" (default) or "ssh"
LAUNCHER_TYPE=local

# --- Local launcher ---
# Path to Unciv.jar inside the container (mounted via docker-compose volume)
UNCIV_JAR_PATH=/opt/unciv/Unciv.jar

# --- SSH launcher (LAUNCHER_TYPE=ssh) ---
# SSH_HOST=
# SSH_USER=
# SSH_KEY_PATH=
# SSH_UNCIV_JAR_PATH=/var/civ_game/Unciv.jar
# SSH_UNCIV_WORK_DIR=/var/civ_game
```

## Running with Docker

### Local launcher (default)

Place `Unciv.jar` next to `docker-compose.yml` — it will be bind-mounted into the container.

If you pass `jar_url` in `/games/start` requests, the jar will be downloaded and replaced automatically before each game creation.

```bash
# Create shared Docker network (once)
docker network create civ_network

# Start the service
docker compose up -d
```

### SSH launcher

Set `LAUNCHER_TYPE=ssh` and fill in the SSH settings in `.env`. The jar lives on the remote host; the service SSHes in to run `Unciv.jar --creategame`.

---

Listens on port **8080** inside the container (mapped to host port **8085** by default).

Internal address for other containers on `civ_network`:

```
http://unciv-game-service:8080
```

## License

MIT
