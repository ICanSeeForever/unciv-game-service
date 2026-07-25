"""SSH game launcher — runs Unciv.jar on a remote host via asyncssh."""
import json

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
        elif settings.ssh_password:
            connect_kwargs["password"] = settings.ssh_password

        async with asyncssh.connect(**connect_kwargs) as conn:
            remote_cfg = "/tmp/unciv_create_config.json"

            # Write config JSON to remote temp file
            await conn.run(
                f"cat > {remote_cfg}",
                input=config_json,
                check=True,
            )

            # Build launch command; wrap in sudo -S if sudo password is set
            jar_cmd = (
                f"cd {settings.ssh_unciv_work_dir} && "
                f"java -jar {settings.ssh_unciv_jar_path} --creategame={remote_cfg}"
            )
            if settings.ssh_sudo_password:
                jar_cmd = f"echo {settings.ssh_sudo_password!r} | sudo -S sh -c {jar_cmd!r}"

            result = await conn.run(jar_cmd, check=True)
            await conn.run(f"rm -f {remote_cfg}")

            # Jar may print game id to stdout or stderr — return both
            return (result.stdout or "") + (result.stderr or "")
