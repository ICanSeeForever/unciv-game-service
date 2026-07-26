# unciv-game-service

REST API microservice for managing [Unciv](https://github.com/yairm210/Unciv) multiplayer games.

Handles all direct interaction with Unciv game files and game creation — so the bot layer never touches the filesystem or SSH directly.

## Features

- **Read game data** — parse save files: current player, turn number, player list, capitals, units, diplomacy
- **Map validation** — check start positions: distances between civs, luxury resources, marine civs on coast, land ratio
- **Async game creation** — launch `Unciv.jar --creategame` via local subprocess or SSH, with automatic map-check retry loop
- **Task polling** — long-running jobs return a `task_id` immediately; poll `GET /tasks/{id}` for progress and logs

## Architecture

```
Bot / Web Client
      │
      ▼
unciv-game-service  (this repo)
      │
      ├── reads/writes  CIV_PATH/MultiplayerFiles/  (volume mount)
      └── SSH launch →  Unciv.jar --creategame      (remote VM)
```

Game files are read directly from the mounted `CIV_PATH/MultiplayerFiles/` directory (the Unciv MP server's data folder), with an HTTP fallback to the MP server if the file is absent locally.

## API

Interactive docs available at `/docs` (Swagger UI) and `/redoc`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/games` | List all local games with human civs and turn info |
| `GET` | `/games/{id}/info` | Current player, turn, full player list with types |
| `GET` | `/games/{id}/map-check` | Validate map quality (distances, luxuries, marine civs, land) |
| `GET` | `/games/{id}/save` | Full parsed save dict |
| `GET` | `/games/{id}/preview` | Parsed preview file |
| `POST` | `/games/start` | Start async game creation (returns `task_id`) |
| `GET` | `/tasks/{task_id}` | Poll async task status and log |
| `GET` | `/health` | Health check |

## Save file format

Unciv saves are `base64(gzip(JSON))`. This service decodes them transparently — all endpoints return plain JSON.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```env
# Path to Unciv MultiplayerFiles on the host (mounted into container at /data/unciv)
CIV_PATH=/path/to/unciv/data

# Default Unciv multiplayer server URL
GAME_HOST=http://your-unciv-server:8084

# Launcher type: "ssh" or "local"
LAUNCHER_TYPE=ssh

# SSH launcher settings (required when LAUNCHER_TYPE=ssh)
SSH_HOST=
SSH_USER=
SSH_PASSWORD=        # or use SSH_KEY_PATH for key auth
SSH_SUDO_PASSWORD=   # if sudo is needed on the remote host
SSH_UNCIV_JAR_PATH=/var/civ_game/Unciv.jar
SSH_UNCIV_WORK_DIR=/var/civ_game
```

## Running with Docker

```bash
# Create shared Docker network (once)
docker network create civ_network

# Start the service
docker compose up -d
```

The service listens on port **8080** inside the container (mapped to host port **8085** by default).

To connect from another container on the same `civ_network`:

```
http://unciv-game-service:8080
```

## Game creation flow

`POST /games/start` accepts a JSON config dict (the same format Unciv.jar expects via `--creategame`) and runs the following loop:

1. Launch `Unciv.jar --creategame` (via SSH or local subprocess)
2. Extract `game_id` from jar output (`Game started successfully with game id: <uuid>`)
3. Read the created save and run map-check
4. If map check fails, restart and try again (up to `MAX_START_ATTEMPTS` attempts)
5. Return `{ game_id }` on success or error details on failure

Poll `GET /tasks/{task_id}` to follow progress and read per-attempt logs.

## License

MIT
