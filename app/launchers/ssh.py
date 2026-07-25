"""SSH game launcher — runs Unciv.jar on a remote host via asyncssh."""
import asyncio
import json
import os

import asyncssh

from app.config import settings
from app.launchers.base import GameLauncher


class SSHGameLauncher(GameLauncher):
    async def launch(self, config: dict) -> str:
        config_json = json.dumps(config, ensure_ascii=False)

        connect_kwargs: dict = {
            "host": settings.ssh_host,
            "username": settings.ssh_user,
            "known_hosts": None,
        }
        if settings.ssh_key_path:
            connect_kwargs["client_keys"] = [settings.ssh_key_path]

        async with asyncssh.connect(**connect_kwargs) as conn:
            # Write config to a temp file on remote, then launch
            remote_cfg = "/tmp/unciv_create_config.json"
            await conn.run(
                f"cat > {remote_cfg} << 'EOFCONFIG'\n{config_json}\nEOFCONFIG",
                check=True,
            )
            result = await conn.run(
                f"java -jar {settings.ssh_unciv_jar_path} --creategame={remote_cfg}",
                check=True,
            )
            await conn.run(f"rm -f {remote_cfg}")
            return result.stdout or ""
