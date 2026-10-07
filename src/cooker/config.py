"""Config loading.

`config.yaml` is the tuning surface. Nothing here should need a code change to
make Cooker more or less aggressive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


class ConfigError(RuntimeError):
    pass


def _expand(value: Any) -> Any:
    """Expand `~` in path-like strings only. Do NOT run every string through
    Path(): it collapses 'http://' to 'http:/' and silently corrupts URLs."""
    if isinstance(value, str):
        return str(Path(value).expanduser()) if value.startswith("~") else value
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class Config:
    """Read-only dotted view over the parsed YAML.

    Typos fail loudly at the point of use rather than silently returning None,
    because a silently-missing budget ceiling is exactly the kind of bug that
    turns a polite sidecar into a noisy neighbour.
    """

    def __init__(self, data: dict[str, Any], root: Path) -> None:
        self._data = data
        self.root = root

    def get(self, dotted: str, default: Any = ...) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is ...:
                    raise ConfigError(f"missing config key: {dotted}")
                return default
            node = node[part]
        return node

    def with_overrides(self, **dotted: Any) -> Config:
        """A copy with specific keys replaced, for benches and tests that need a
        three-second cooldown instead of sixty.

        Deep-copied, so shortening a timeout cannot mutate the caller's config.
        An override naming a key that does not exist is a refusal rather than an
        addition: a typo in `infer_min_bps` would otherwise create a knob nothing
        reads while the real one stayed at its default.
        """
        import copy

        data = copy.deepcopy(self._data)
        for key, value in dotted.items():
            node: Any = data
            parts = key.split(".")
            for part in parts[:-1]:
                if not isinstance(node, dict) or part not in node:
                    raise ConfigError(f"override for unknown key: {key}")
                node = node[part]
            if not isinstance(node, dict) or parts[-1] not in node:
                raise ConfigError(f"override for unknown key: {key}")
            node[parts[-1]] = value
        return Config(data, self.root)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)
        if isinstance(value, dict):
            return Config(value, self.root)
        return value

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        return self[key]

    def __contains__(self, key: str) -> bool:
        try:
            self.get(key)
            return True
        except ConfigError:
            return False

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    # --- convenience paths -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        p = Path(self.get("daemon.data_dir", "var"))
        return p if p.is_absolute() else self.root / p

    @property
    def outputs_dir(self) -> Path:
        """Where published artifacts land. Relative means *inside `data_dir`*.

        This used to resolve against the config root, while `chains.day_dir` built
        `data_dir/outputs` itself — so the daemon wrote 212 KB into `var/outputs/`
        and `cooker doctor` cheerfully reported "0.0 MB of 2.0 GB" about an empty
        `outputs/` beside it. A quota measured against the wrong directory is not a
        quota, and the two definitions had to collapse into one.
        """
        p = Path(self.get("safety.outputs_dir", "outputs"))
        return p if p.is_absolute() else self.data_dir / p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cooker.db"


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"config not found: {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a mapping: {cfg_path}")
    root = cfg_path.resolve().parent
    return Config(_expand(data), root)
