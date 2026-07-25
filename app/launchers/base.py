"""Abstract game launcher interface."""
import abc
import json
import tempfile
from pathlib import Path


class GameLauncher(abc.ABC):
    @abc.abstractmethod
    async def launch(self, config: dict) -> str:
        """Launch Unciv game creation with given config.

        Returns stdout output from the jar invocation.
        """

    def _write_config_tmp(self, config: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(config, tmp, ensure_ascii=False)
        tmp.close()
        return tmp.name
