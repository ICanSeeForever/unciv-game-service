from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Unciv MultiplayerFiles directory mount
    civ_path: str = "/data/unciv"

    # Default Unciv MP server (can be overridden per-request via config body)
    game_host: str = ""

    # Launcher: "local" or "ssh"
    launcher_type: str = "ssh"

    # Path to Unciv.jar (used by LocalGameLauncher)
    unciv_jar_path: str = "/opt/unciv/Unciv.jar"

    # SSH settings (used by SSHGameLauncher)
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_key_path: str = ""
    ssh_unciv_jar_path: str = "~/Unciv.jar"

    # Map check thresholds (can be overridden by env)
    expect_min_distance: int = 10
    expect_max_distance: int = 16
    expect_min_luxuries: int = 5
    expect_min_unique_luxuries: int = 1
    expect_min_land_per_player: int = 185
    expect_max_land_per_player: int = 200
    max_start_attempts: int = 30

    # Task TTL: how long to keep completed/failed tasks (seconds)
    task_ttl: int = 3600

    # Path to static game data (cs, marine civs)
    static_data_path: str = "data/static.json"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
