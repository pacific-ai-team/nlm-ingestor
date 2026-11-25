"""Portable settings for measured test shards and workflow-scoped recovery."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
UPLOAD_INTERVAL_SECONDS = 15
STORAGE_TIMEOUT_SECONDS = 20
SQLITE_TIMEOUT_SECONDS = 30
HISTORY_SAMPLES = 20
UNKNOWN_TEST_SECONDS = 1.0


@dataclass(frozen=True)
class RuntimeSettings:
    store: str
    suite: str
    revision: str
    run: str
    cutoff: float
    shard: int = 0
    shards: int = 1
    state: Path = Path(".ci-runtime")
    context: str = ""
    credential_source: str = "irsa"
    history_aliases: Path | None = None
    history_file: Path | None = None
    split_files: tuple[str, ...] = ()
    single_worker: bool = False

    def __post_init__(self) -> None:
        if self.single_worker and (self.shards != 1 or self.history_file is not None):
            raise ValueError(
                "single-worker recovery requires one shard and no history file"
            )
        if self.shards < 1 or not 0 <= self.shard < self.shards:
            raise ValueError("CI_RUNTIME_SHARD must be in [0, CI_RUNTIME_SHARDS)")
        if not all((self.store, self.suite, self.revision, self.run)):
            raise ValueError("store, suite, revision and workflow run are required")
        if self.cutoff <= 0:
            raise ValueError("a shared workflow creation time is required")
        if any(
            not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or "::" in name
            for name in self.split_files
        ) or len(set(self.split_files)) != len(self.split_files):
            raise ValueError("split files must be distinct repository-relative paths")
        if self.credential_source not in ("irsa", "environment"):
            raise ValueError("credential source must be irsa or environment")

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        cutoff = os.environ["CI_RUNTIME_EPOCH"]
        single_worker = os.environ.get("CI_RUNTIME_SINGLE_WORKER", "0")
        if single_worker not in ("0", "1"):
            raise ValueError("CI_RUNTIME_SINGLE_WORKER must be 0 or 1")
        return cls(
            store=os.environ["CI_RUNTIME_STORE"],
            single_worker=single_worker == "1",
            suite=os.environ["CI_RUNTIME_SUITE"],
            revision=os.environ["CI_RUNTIME_REVISION"],
            run=os.environ["CI_RUNTIME_RUN"],
            cutoff=datetime.fromisoformat(cutoff.replace("Z", "+00:00")).timestamp(),
            shard=int(os.environ.get("CI_RUNTIME_SHARD", "0")),
            shards=int(os.environ.get("CI_RUNTIME_SHARDS", "1")),
            state=Path(os.environ.get("CI_RUNTIME_STATE", ".ci-runtime")),
            context=os.environ.get("CI_RUNTIME_CONTEXT", "")
            + ":"
            + platform.system()
            + ":"
            + platform.python_version(),
            credential_source=os.environ.get("CI_RUNTIME_CREDENTIAL_SOURCE", "irsa"),
            split_files=tuple(
                sorted(os.environ.get("CI_RUNTIME_SPLIT_FILES", "").split())
            ),
            history_aliases=(
                Path(os.environ["CI_RUNTIME_HISTORY_ALIASES"])
                if os.environ.get("CI_RUNTIME_HISTORY_ALIASES")
                else None
            ),
            history_file=(
                Path(os.environ["CI_RUNTIME_HISTORY_FILE"])
                if os.environ.get("CI_RUNTIME_HISTORY_FILE")
                else None
            ),
        )
