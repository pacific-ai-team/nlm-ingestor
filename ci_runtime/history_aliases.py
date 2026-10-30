"""Canonicalize obsolete test paths in performance history, never recovery state."""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate history alias JSON key: {key}")
        result[key] = value
    return result


class HistoryAliases:
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases

    @classmethod
    def load(cls, filename: Path | None) -> HistoryAliases:
        if filename is None:
            return cls({})
        data = json.loads(filename.read_text(), object_pairs_hook=_unique_pairs)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "aliases"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["aliases"], list)
        ):
            raise ValueError("invalid history alias manifest")
        aliases: dict[str, str] = {}
        for pair in data["aliases"]:
            if not isinstance(pair, dict) or set(pair) != {"before", "after"}:
                raise ValueError("history aliases require before and after paths")
            before, after = pair["before"], pair["after"]
            for name in (before, after):
                if (
                    not isinstance(name, str)
                    or not name
                    or name == "."
                    or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts
                    or PurePosixPath(name).as_posix() != name
                    or "\\" in name
                    or "::" in name
                ):
                    raise ValueError(
                        "history aliases require repository-relative paths"
                    )
            if before == after or before in aliases or after in aliases.values():
                raise ValueError("history aliases must be distinct one-to-one moves")
            aliases[before] = after
        if set(aliases) & set(aliases.values()):
            raise ValueError("history aliases must not contain chains or cycles")
        return cls(aliases)

    def nodeid(self, nodeid: str) -> str:
        if not isinstance(nodeid, str):
            raise TypeError("history node ids must be strings")
        module, separator, suffix = nodeid.partition("::")
        return self.aliases.get(module, module) + separator + suffix

    def measurements(self, values: dict[str, float]) -> dict[str, float]:
        """An old/current collision is one observation, retaining its maximum."""
        if not isinstance(values, dict):
            raise TypeError("history measurements must be a mapping")
        result: dict[str, float] = {}
        for nodeid, value in values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("invalid history measurement")
            canonical = self.nodeid(nodeid)
            result[canonical] = max(result.get(canonical, value), value)
        return result
