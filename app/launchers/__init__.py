from app.config import settings
from app.launchers.base import GameLauncher


def get_launcher() -> GameLauncher:
    if settings.launcher_type == "local":
        from app.launchers.local import LocalGameLauncher
        return LocalGameLauncher()
    elif settings.launcher_type == "ssh":
        from app.launchers.ssh import SSHGameLauncher
        return SSHGameLauncher()
    raise ValueError(f"Unknown LAUNCHER_TYPE: {settings.launcher_type!r}")
