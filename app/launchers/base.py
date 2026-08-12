"""Abstract game launcher interface."""
import abc
import json
import tempfile


class GameLauncher(abc.ABC):
    @abc.abstractmethod
    async def launch(self, config: dict) -> str:
        """Launch Unciv game creation with given config.

        Returns stdout output from the jar invocation.
        """

    async def update(self, jar_url: str) -> None:
        """Download/update Unciv.jar before game creation. Default is a no-op."""

    async def clone_mod(self, mod_git_url: str) -> None:
        """Fresh-clone mod into Unciv mods directory. Default is a no-op."""

    def _write_config_tmp(self, config: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(config, tmp, ensure_ascii=False)
        tmp.close()
        return tmp.name
