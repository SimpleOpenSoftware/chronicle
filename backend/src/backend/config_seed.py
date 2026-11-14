"""Seed a writable config directory from a read-only copy, keeping live settings.

In Kubernetes the repository's config files arrive in a ConfigMap, which is
read-only. Runtime settings changes (the Settings page, ``save_config_section``)
must be written somewhere, so the backend's ``CONFIG_DIR`` points at a
directory on the data volume instead, as the shared bind mount does under
Docker Compose. An init container runs this module before the backend and
workers start, on every pod start:

* ``defaults.yml`` is shipped code defaults, not settings: always replaced.
* ``config.yml`` and ``plugins.yml`` hold settings. When absent they are
  copied. When present, keys that exist only in the seed are added and every
  key already in the live file keeps its live value, so a redeploy never
  undoes a change made in the app. A list is a single value: a live list is
  kept whole, never merged element by element.

Usage: python -m backend.config_seed SEED_DIR CONFIG_DIR
"""

import os
import shutil
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

ALWAYS_REPLACED = ("defaults.yml",)
MERGED = ("config.yml", "plugins.yml")


def merge_missing(live: dict, seed: dict, prefix: str = "") -> list[str]:
    """Add the keys of ``seed`` that ``live`` lacks, in place; live values win.

    Returns the dotted paths of the keys that were added.
    """
    added = []
    for key, seed_value in seed.items():
        path = f"{prefix}{key}"
        if key not in live:
            live[key] = seed_value
            added.append(path)
        elif isinstance(live[key], dict) and isinstance(seed_value, dict):
            added.extend(merge_missing(live[key], seed_value, f"{path}."))
    return added


def _atomic_write(target: Path, write) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        write(stream)
    os.replace(temporary, target)


def seed_config_dir(seed_dir: Path, config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.preserve_quotes = True

    for name in ALWAYS_REPLACED:
        source = seed_dir / name
        if not source.exists():
            raise FileNotFoundError(
                f"{source} is missing from the seed; run 'make config-k8s'"
            )
        _atomic_write(
            config_dir / name, lambda f: f.write(source.read_text(encoding="utf-8"))
        )
        print(f"{name}: replaced from seed")

    for name in MERGED:
        source = seed_dir / name
        target = config_dir / name
        if not source.exists():
            print(f"{name}: not in seed, left as is")
            continue
        if not target.exists():
            shutil.copyfile(source, target)
            print(f"{name}: created from seed")
            continue
        live = yaml.load(target.read_text(encoding="utf-8")) or CommentedMap()
        seed = yaml.load(source.read_text(encoding="utf-8")) or {}
        added = merge_missing(live, seed)
        if not added:
            print(f"{name}: live settings kept, nothing new in seed")
            continue
        _atomic_write(target, lambda f: yaml.dump(live, f))
        print(f"{name}: live settings kept, added from seed: {', '.join(added)}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    seed_config_dir(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
