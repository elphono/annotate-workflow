"""Configuration: where the data lives, which port the daemon listens on.

Nothing about the deployment is written in the code (no home directory, no
distribution name, no port): every value comes from the environment, from
`~/.config/annotate/config.toml`, or from a default that is not tied to a
machine. A test walks the AST of the package to keep it that way.

Precedence, highest first: environment variable, config file, default.

| Field          | Env variable             | Default                          |
|----------------|--------------------------|----------------------------------|
| port           | ANNOTATE_PORT            | 8765                             |
| send_timeout   | ANNOTATE_SEND_TIMEOUT    | 1800 seconds                     |
| claude_bin     | ANNOTATE_CLAUDE          | "claude" (resolved on the PATH)  |
| data_dir       | ANNOTATE_DATA_DIR        | ~/.local/share/annotate          |
| (config file)  | ANNOTATE_CONFIG          | ~/.config/annotate/config.toml   |

`claude_bin` exists for one reason beyond convenience: the end-to-end test
points it at a fake executable whose name is NOT `claude`, so the test suite
guard (which refuses any real `claude`) lets it run.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8765
DEFAULT_SEND_TIMEOUT = 1800


class ConfigError(Exception):
    """The configuration file or an environment override is invalid."""


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    send_timeout: int = DEFAULT_SEND_TIMEOUT
    claude_bin: str = "claude"


def config_path() -> Path:
    env = os.environ.get("ANNOTATE_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".config" / "annotate" / "config.toml"


def data_dir() -> Path:
    env = os.environ.get("ANNOTATE_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "annotate"


def _as_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    try:
        number = int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {number}")
    return number


def load_config() -> Config:
    """Read the config file (optional), then apply environment overrides."""
    raw: dict[str, object] = {}
    path = config_path()
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    port: object = raw.get("port", DEFAULT_PORT)
    timeout: object = raw.get("send_timeout", DEFAULT_SEND_TIMEOUT)
    claude: object = raw.get("claude_bin", "claude")

    port = os.environ.get("ANNOTATE_PORT", port)
    timeout = os.environ.get("ANNOTATE_SEND_TIMEOUT", timeout)
    claude = os.environ.get("ANNOTATE_CLAUDE", claude)

    if not isinstance(claude, str) or not claude.strip():
        raise ConfigError(f"claude_bin must be a non-empty string, got {claude!r}")
    return Config(port=_as_int("port", port, 1, 65535),
                  send_timeout=_as_int("send_timeout", timeout, 10, 24 * 3600),
                  claude_bin=claude.strip())
