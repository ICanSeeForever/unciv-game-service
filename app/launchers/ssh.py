"""SSH game launcher — runs Unciv.jar on a remote host via asyncssh."""
import json

import asyncssh

from app.config import settings
from app.launchers.base import GameLauncher


class SSHGameLauncher(GameLauncher):
    def _connect_kwargs(self) -> dict:
        kwargs: dict = {
            "host": settings.ssh_host,
            "username": settings.ssh_user,
            "known_hosts": None,
        }
        if settings.ssh_key_path:
            kwargs["client_keys"] = [settings.ssh_key_path]
        elif settings.ssh_password:
            kwargs["password"] = settings.ssh_password
        return kwargs

    def _sudo(self, cmd: str) -> str:
        """Wrap command with sudo -S if sudo password is configured."""
        if settings.ssh_sudo_password:
            return f"echo {settings.ssh_sudo_password!r} | sudo -S sh -c {cmd!r}"
        return cmd

    async def update(self, jar_url: str, mod_git_url: str | None = None) -> None:
        """Download Unciv zip to remote host, unzip, and optionally clone mod."""
        work_dir = settings.ssh_unciv_work_dir

        async with asyncssh.connect(**self._connect_kwargs()) as conn:
            # Clear and recreate work dir
            await conn.run(
                self._sudo(f"rm -rf {work_dir}/* && mkdir -p {work_dir}"),
                check=True,
            )

            # Download zip
            await conn.run(
                self._sudo(f"wget -q -P {work_dir}/ {jar_url}"),
                check=True,
            )

            # Unzip and remove archive
            await conn.run(
                self._sudo(f"sh -c 'cd {work_dir} && unzip -o *.zip && rm *.zip'"),
                check=True,
            )

            if mod_git_url:
                # Parse https://github.com/user/Mod/tree/branch
                parts = mod_git_url.split("/tree/")
                base_url = parts[0].rstrip("/")
                branch = parts[1] if len(parts) > 1 else "main"
                mod_name = base_url.split("/")[-1].replace("-", " ")

                await conn.run(
                    self._sudo(f"mkdir -p '{work_dir}/mods/{mod_name}'"),
                    check=True,
                )
                await conn.run(
                    self._sudo(
                        f"sh -c 'cd \"{work_dir}/mods/{mod_name}\" "
                        f"&& git clone --branch {branch} --depth 1 {base_url}.git . "
                        f"&& rm -rf .git'"
                    ),
                    check=True,
                )

    async def launch(self, config: dict) -> str:
        config_json = json.dumps(config, ensure_ascii=False)
        work_dir = settings.ssh_unciv_work_dir
        jar_path = settings.ssh_unciv_jar_path

        async with asyncssh.connect(**self._connect_kwargs()) as conn:
            remote_cfg = "/tmp/unciv_create_config.json"

            await conn.run(
                f"cat > {remote_cfg}",
                input=config_json,
                check=True,
            )

            jar_cmd = (
                f"cd {work_dir} && "
                f"java -jar {jar_path} --creategame={remote_cfg}"
            )
            if settings.ssh_sudo_password:
                jar_cmd = f"echo {settings.ssh_sudo_password!r} | sudo -S sh -c {jar_cmd!r}"

            try:
                result = await conn.run(jar_cmd, check=True)
            except asyncssh.ProcessError as exc:
                output = ((exc.stderr or "") + (exc.stdout or "")).strip()
                details = output or f"exit status {exc.exit_status}"
                raise RuntimeError(details) from exc
            await conn.run(f"rm -f {remote_cfg}")

            return (result.stdout or "") + (result.stderr or "")
