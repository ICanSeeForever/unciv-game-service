"""Local game launcher — runs Unciv.jar as a subprocess."""
import asyncio
import os

from app.config import settings
from app.launchers.base import GameLauncher


class LocalGameLauncher(GameLauncher):
    async def launch(self, config: dict) -> str:
        cfg_path = self._write_config_tmp(config)
        try:
            proc = await asyncio.create_subprocess_exec(
                "java", "-jar", settings.unciv_jar_path,
                f"--creategame={cfg_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeError(f"Unciv.jar exited {proc.returncode}:\n{output}")
            return output
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
