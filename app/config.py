from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Unciv MultiplayerFiles directory mount
    civ_path: str = "/data/unciv"

    # Default Unciv MP server (can be overridden per-request via config body)
    game_host: str = ""

    # Launcher: "local" or "ssh"
    launcher_type: str = "local"

    # Path to Unciv.jar (used by LocalGameLauncher)
    unciv_jar_path: str = "/opt/unciv/Unciv.jar"

    # Unciv data directory where mods are stored (used by LocalGameLauncher)
    unciv_data_dir: str = "/root/.local/share/Unciv"

    # SSH settings (used by SSHGameLauncher)
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_key_path: str = ""        # path to private key (optional)
    ssh_password: str = ""        # password auth (optional, used if key not set)
    ssh_sudo_password: str = ""   # sudo -S password on remote (if needed)
    ssh_unciv_jar_path: str = "/var/civ_game/Unciv.jar"
    ssh_unciv_work_dir: str = "/var/civ_game"

    # Map check thresholds (can be overridden by env)
    expect_min_distance: int = 10
    expect_max_distance: int = 16
    expect_min_luxuries: int = 5
    expect_min_unique_luxuries: int = 1
    expect_min_land_per_player: int = 185
    expect_max_land_per_player: int = 200
    max_start_attempts: int = 30

    # Backup directory (absolute path inside container)
    backup_path: str = ""  # defaults to {civ_path}/backups when empty

    # Task TTL: how long to keep completed/failed tasks (seconds)
    task_ttl: int = 3600

    # API key for all endpoints (leave empty to disable auth — dev/local only)
    api_key: str = ""

    def get_backup_path(self) -> str:
        return self.backup_path or f"{self.civ_path}/backups"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
