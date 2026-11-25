"""Publish completed shard coverage atomically; merge only a complete run."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import asdict
from pathlib import Path

from coverage import CoverageData

from ci_runtime.config import SCHEMA_VERSION, RuntimeSettings
from ci_runtime.store import RuntimeStore
from ci_runtime.types import CoverageManifest


class CoverageArtifacts:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.store = RuntimeStore(settings)
        self.state = settings.state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)

    def key(self, shard: int) -> str:
        return f"coverage/{self.settings.suite}/{self.settings.run}/{shard}.tgz"

    def publish(self) -> None:
        source = Path(os.environ.get("COVERAGE_FILE", ".coverage")).resolve()
        data = CoverageData(basename=str(source))
        if not source.is_file():
            if not (self.state / "empty-ok").is_file():
                raise ValueError("completed shard has no coverage data")
            data.add_lines({})
            data.write()
        data.read()
        payload = source.read_bytes()
        manifest = CoverageManifest(
            SCHEMA_VERSION,
            self.settings.suite,
            self.settings.revision,
            self.settings.run,
            self.settings.shard,
            self.settings.shards,
            hashlib.sha256(payload).hexdigest(),
        )
        archive = self.state / "complete-coverage.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, content in (
                ("manifest.json", json.dumps(asdict(manifest)).encode()),
                ("coverage", payload),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
        self.store.upload(archive, self.key(self.settings.shard))

    def merge(self, output: Path) -> None:
        # Build a scratch trace and publish only after ALL shards validate.
        # CoverageData.update can write eagerly; never point it at the report
        # destination until the full run is verified.
        pending = self.state / "merged.pending"
        merged = CoverageData(basename=str(pending))
        merged.erase()
        for shard in range(self.settings.shards):
            archive = self.state / f"complete-{shard}.tgz"
            if not self.store.download(self.key(shard), archive):
                raise ValueError(f"missing completed coverage for shard {shard}")
            with tarfile.open(archive) as tar:
                handle = tar.extractfile("manifest.json")
                if handle is None:
                    raise ValueError("missing coverage manifest")
                manifest = CoverageManifest(**json.load(handle))
                expected = (
                    SCHEMA_VERSION,
                    self.settings.suite,
                    self.settings.revision,
                    self.settings.run,
                    shard,
                    self.settings.shards,
                )
                actual = (
                    manifest.version,
                    manifest.suite,
                    manifest.revision,
                    manifest.run,
                    manifest.shard,
                    manifest.shards,
                )
                if actual != expected:
                    raise ValueError(f"coverage identity mismatch for shard {shard}")
                handle = tar.extractfile("coverage")
                if handle is None:
                    raise ValueError("missing coverage data")
                payload = handle.read()
                if hashlib.sha256(payload).hexdigest() != manifest.sha256:
                    raise ValueError("coverage checksum mismatch")
                source = self.state / f"coverage-{shard}"
                source.write_bytes(payload)
                data = CoverageData(basename=str(source))
                data.read()
                merged.update(data)
        merged.write()
        os.replace(pending, output.resolve())
