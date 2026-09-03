"""Local game launcher — runs Unciv.jar as a subprocess."""
import asyncio
import io
import os
import shutil
import zipfile
from pathlib import Path

import httpx

from app.config import settings
from app.launchers.base import GameLauncher


class LocalGameLauncher(GameLauncher):
    async def update(self, jar_url: str) -> None:
        """Download zip from jar_url and extract Unciv.jar."""
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

    async def clone_mod(self, mod_git_url: str) -> None:
        """Fresh-clone the mod into the Unciv mods directory, wiping any existing copy.

        mod_git_url format: https://github.com/Owner/Repo-name/tree/branch
        Mod directory name is derived from the repo name (hyphens → spaces).
        """
        parts = mod_git_url.split("/tree/")
        repo_url = parts[0].rstrip("/") + ".git"
        branch = parts[1] if len(parts) > 1 else "main"

        repo_name = repo_url.rstrip(".git").rstrip("/").rsplit("/", 1)[-1]
        mod_name = repo_name.replace("-", " ")

        mods_dir = Path(settings.unciv_jar_path).parent / "mods"
        # Полностью чистим mods: в игре ровно один рулсет-мод, чтобы рулсеты от
        # прошлых игр (в т.ч. другого типа — G&K) не подмешивались.
        if mods_dir.exists():
            shutil.rmtree(mods_dir)
        mods_dir.mkdir(parents=True, exist_ok=True)
        mod_dir = mods_dir / mod_name
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

    async def prepare_builtin_ruleset(self) -> None:
        """Подготовить игру на встроенном базовом рулсете (Vanilla / G&K) — без мода.

        В console-режиме (``--creategame``) Unciv читает встроенные рулсеты НЕ из
        jar, а с ДИСКА рядом с jar: ``getRulesetFile`` = ``FileHandle("jsons/…")``
        (RulesetCache.kt). Без этой папки рулсет грузится без ``speeds`` → краш на
        ``ruleset.speeds.first()``. Поэтому выкладываем ``jsons/`` прямо из jar
        (версии совпадают). Мод-папка при этом не нужна — чистим mods.
        """
        jar_dir = Path(settings.unciv_jar_path).parent
        mods_dir = jar_dir / "mods"
        if mods_dir.exists():
            shutil.rmtree(mods_dir)
        mods_dir.mkdir(parents=True, exist_ok=True)
        jsons_dir = jar_dir / "jsons"
        if jsons_dir.exists():
            shutil.rmtree(jsons_dir)
        count = 0
        with zipfile.ZipFile(settings.unciv_jar_path) as z:
            for entry in z.namelist():
                if entry.startswith("jsons/") and not entry.endswith("/"):
                    dst = jar_dir / entry
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(z.read(entry))
                    count += 1
        if count == 0:
            raise RuntimeError("no builtin jsons/ found in Unciv.jar")
        print(f"Prepared builtin rulesets from jar: {count} json files")

    async def launch(self, config: dict) -> str:
        cfg_path = self._write_config_tmp(config)
        jar_dir = str(Path(settings.unciv_jar_path).parent)
        try:
            proc = await asyncio.create_subprocess_exec(
                "java", "-jar", settings.unciv_jar_path,
                f"--creategame={cfg_path}",
                cwd=jar_dir,
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
