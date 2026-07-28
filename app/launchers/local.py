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
        """Download zip from jar_url and extract Unciv.jar; clone mod if provided."""
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

        if mod_git_url:
            await self._clone_mod(mod_git_url)

    async def _clone_mod(self, mod_git_url: str) -> None:
        """Clone or update the mod into the Unciv mods directory.

        mod_git_url format: https://github.com/Owner/Repo-name/tree/branch
        Mod directory name is derived from the repo name (hyphens → spaces).
        """
        # Parse URL: strip /tree/<branch> to get repo URL and branch
        parts = mod_git_url.split("/tree/")
        repo_url = parts[0].rstrip("/") + ".git"
        branch = parts[1] if len(parts) > 1 else "main"

        # Derive mod name from repo name (last path segment, hyphens → spaces)
        repo_name = repo_url.rstrip(".git").rstrip("/").rsplit("/", 1)[-1]
        mod_name = repo_name.replace("-", " ")

        mods_dir = Path(settings.unciv_data_dir) / "mods"
        mod_dir = mods_dir / mod_name

        if mod_dir.exists():
            # Pull latest
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(mod_dir), "pull", "--ff-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            print(f"Mod pull ({mod_name}): {stdout.decode('utf-8', errors='replace').strip()}")
        else:
            mods_dir.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--branch", branch, "--depth", "1",
                repo_url, str(mod_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            print(f"Mod clone ({mod_name}): {stdout.decode('utf-8', errors='replace').strip()}")
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed:\n{stdout.decode('utf-8', errors='replace')}")

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
