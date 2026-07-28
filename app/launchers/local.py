"""Local game launcher — runs Unciv.jar as a subprocess."""
import asyncio
import io
import os
import zipfile
from pathlib import Path

import httpx

from app.config import settings
from app.launchers.base import GameLauncher


class LocalGameLauncher(GameLauncher):
    async def update(self, jar_url: str, mod_git_url: str | None = None) -> None:
        """Download zip from jar_url and extract Unciv.jar to settings.unciv_jar_path."""
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            resp = await client.get(jar_url)
            resp.raise_for_status()
            data = resp.content

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            jar_entries = [n for n in z.namelist() if n.endswith(".jar")]
            if not jar_entries:
                raise RuntimeError("No .jar found in downloaded zip")
            jar_path = Path(settings.unciv_jar_path)
            jar_path.parent.mkdir(parents=True, exist_ok=True)
            jar_path.write_bytes(z.read(jar_entries[0]))

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
