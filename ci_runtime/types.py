"""Versioned manifests exchanged by CI shards."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoredObject:
    key: str
    modified: float


@dataclass
class RuntimeManifest:
    version: int
    identity: str
    collection: list[str]
    completed: list[str]
    seconds: dict[str, float]
    coverage_required: bool
    coverage_file: bool


@dataclass
class RuntimeHistory:
    version: int
    seconds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageManifest:
    version: int
    suite: str
    revision: str
    run: str
    shard: int
    shards: int
    sha256: str


@dataclass(frozen=True)
class RuntimePlan:
    version: int
    suite: str
    revision: str
    run: str
    shards: int
    cutoff: float
    seconds: dict[str, float]
