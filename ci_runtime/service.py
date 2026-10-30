"""Deterministic packing and atomic passed-test + coverage checkpoints.

Shard membership uses only history older than the common workflow creation
instant. A checkpoint is one atomic object: its journal can never outrun its
coverage. Files are the default recovery unit: module-scoped state is replayed unless
every selected test in that file passed. Explicitly independent files can opt
into item-level packing and recovery through split_files.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import statistics
import subprocess
import tarfile
import threading
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from coverage import CoverageData
from coverage.exceptions import CoverageException

from ci_runtime.config import (
    HISTORY_SAMPLES,
    SCHEMA_VERSION,
    SQLITE_TIMEOUT_SECONDS,
    UNKNOWN_TEST_SECONDS,
    UPLOAD_INTERVAL_SECONDS,
    RuntimeSettings,
)
from ci_runtime.store import RuntimeStore
from ci_runtime.types import RuntimeHistory, RuntimeManifest, RuntimePlan


class RuntimeService:
    def __init__(
        self, settings: RuntimeSettings, invocation: str, coverage_required: bool
    ) -> None:
        self.settings = settings
        self.store = RuntimeStore(settings)
        self.state = settings.state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        identity = json.dumps(
            [
                SCHEMA_VERSION,
                settings.suite,
                settings.revision,
                settings.context,
                invocation,
                settings.shards,
                settings.shard,
                sorted(settings.split_files),
            ]
        )
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.checkpoint_key = (
            f"checkpoints/{settings.suite}/{settings.run}/{self.identity}.tgz"
        )
        self.db = self.state / "reports.sqlite"
        self.prior = self.state / "coverage.prior"
        self.coverage_required = coverage_required
        self.collection: list[str] = []
        self.restored: set[str] = set()
        self.history: dict[str, float] = {}
        self.prior_seconds: dict[str, float] = {}
        self.stop = threading.Event()
        self.uploader: threading.Thread | None = None
        self.publish_lock = threading.Lock()

    @staticmethod
    def groups(
        nodeids: list[str], split_files: tuple[str, ...]
    ) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for nodeid in sorted(nodeids):
            filename = nodeid.split("::", 1)[0]
            groups[nodeid if filename in split_files else filename].append(nodeid)
        return groups

    @staticmethod
    def pack(
        nodeids: list[str],
        seconds: dict[str, float],
        shards: int,
        split_files: tuple[str, ...] = (),
    ) -> list[list[str]]:
        if shards < 1 or len(set(nodeids)) != len(nodeids):
            raise ValueError("positive shard count and distinct test ids are required")
        groups = RuntimeService.groups(nodeids, split_files)
        measured = [s for s in seconds.values() if math.isfinite(s) and s >= 0]
        default = statistics.median(measured) if measured else UNKNOWN_TEST_SECONDS
        weights = {
            name: sum(seconds.get(n, default) for n in ids)
            for name, ids in groups.items()
        }
        loads = [0.0] * shards
        assignments: list[list[str]] = [[] for _ in range(shards)]
        # Bootstrap explicit independent batches before ordinary file groups:
        # unknown tiny defaults must not all fill one large file's load gap.
        # Once measured, every group participates in ordinary global LPT.
        bootstrap = {
            name
            for name, ids in groups.items()
            if name.split("::", 1)[0] in split_files
            and any(nodeid not in seconds for nodeid in ids)
        }
        for name in sorted(groups, key=lambda n: (n not in bootstrap, -weights[n], n)):
            shard = min(range(shards), key=lambda i: (loads[i], i))
            assignments[shard].extend(groups[name])
            loads[shard] += weights[name]
        return [sorted(ids) for ids in assignments]

    def load_history(self) -> None:
        if self.settings.history_file is not None:
            plan = RuntimePlan(
                **json.loads(self.settings.history_file.read_text(encoding="utf-8"))
            )
            if plan.version != SCHEMA_VERSION:
                raise ValueError("unsupported frozen history plan")
            if (plan.suite, plan.run, plan.revision, plan.shards, plan.cutoff) != (
                self.settings.suite,
                self.settings.run,
                self.settings.revision,
                self.settings.shards,
                self.settings.cutoff,
            ):
                raise ValueError("frozen history plan belongs to another run")
            self.history = plan.seconds
            if any(
                not isinstance(s, (int, float)) or not math.isfinite(s) or s < 0
                for s in self.history.values()
            ):
                raise ValueError("invalid frozen history plan")
            return
        if self.settings.store.startswith("github://") and not os.environ.get(
            "CI_RUNTIME_BUILDING_PLAN"
        ):
            raise ValueError(
                "GitHub matrix shards require one shared frozen history plan"
            )
        # A transport error fails selection. Falling back independently in each
        # pod would produce different plans and leave some tests unassigned.
        objects = sorted(
            (
                o
                for o in self.store.objects(f"history/{self.settings.suite}/")
                if o.modified < self.settings.cutoff
            ),
            key=lambda o: (o.modified, o.key),
            reverse=True,
        )[:HISTORY_SAMPLES]
        samples: dict[str, list[float]] = defaultdict(list)
        for index, obj in enumerate(objects):
            dest = self.state / f"history-{index}.json"
            if not self.store.download(obj.key, dest):
                raise OSError(
                    "timing history disappeared while constructing the shard plan"
                )
            raw = json.loads(dest.read_text(encoding="utf-8"))
            if raw["version"] != SCHEMA_VERSION:
                raise ValueError("unsupported CI timing history version")
            for nodeid, seconds in raw["seconds"].items():
                if (
                    not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds)
                    or seconds < 0
                ):
                    raise ValueError("invalid timing history")
                samples[nodeid].append(seconds)
        self.history = {n: statistics.median(s) for n, s in samples.items()}
        print(
            f"CI_RUNTIME history={len(objects)} objects tests={len(self.history)}",
            flush=True,
        )

    def initialise(self) -> None:
        # This directory is attempt-local (emptyDir/RUNNER_TEMP), never a
        # dependency cache. Only a validated remote manifest can skip a test.
        with sqlite3.connect(self.db, timeout=SQLITE_TIMEOUT_SECONDS) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reports "
                "(nodeid TEXT, stage TEXT, passed INTEGER, seconds REAL, "
                "PRIMARY KEY(nodeid, stage))"
            )
            db.execute("DELETE FROM reports")
        (self.state / "collection.json").unlink(missing_ok=True)
        (self.state / "empty-ok").unlink(missing_ok=True)
        self.restore()

    def restore(self) -> None:
        archive = self.state / "restore.tgz"
        if not self.store.download(self.checkpoint_key, archive):
            return
        try:
            with tarfile.open(archive, "r:gz") as tar:
                handle = tar.extractfile("manifest.json")
                if handle is None:
                    raise ValueError("checkpoint has no manifest")
                manifest = RuntimeManifest(**json.load(handle))
                if (
                    manifest.version != SCHEMA_VERSION
                    or manifest.identity != self.identity
                ):
                    raise ValueError("checkpoint does not match this invocation")
                if not set(manifest.completed) <= set(manifest.collection):
                    raise ValueError("checkpoint contains uncollected tests")
                if self.coverage_required and not manifest.coverage_file:
                    raise ValueError("checkpoint is missing required coverage")
                if manifest.coverage_file:
                    handle = tar.extractfile("coverage")
                    if handle is None:
                        raise ValueError("checkpoint coverage is missing")
                    self.prior.write_bytes(handle.read())
                    data = CoverageData(basename=str(self.prior))
                    data.read()
                    if self.coverage_required and not data.measured_files():
                        raise ValueError("checkpoint coverage is empty")
                self.restored = set(manifest.completed)
                self.collection = manifest.collection
                self.prior_seconds = manifest.seconds
        except (
            ValueError,
            TypeError,
            KeyError,
            tarfile.TarError,
            CoverageException,
            sqlite3.DatabaseError,
        ) as exc:
            # Rerunning is safe; accepting a partial checkpoint is not.
            self.restored.clear()
            self.collection = []
            self.prior.unlink(missing_ok=True)
            print(f"CI_RUNTIME ignoring invalid checkpoint: {exc}", flush=True)

    def select(self, nodeids: list[str]) -> tuple[set[str], set[str]]:
        assigned = self.pack(
            nodeids, self.history, self.settings.shards, self.settings.split_files
        )[self.settings.shard]
        # Persisted membership includes the exact post-marker collection. A
        # changed collection on a retry invalidates all resume decisions.
        if self.collection and self.collection != assigned:
            self.restored.clear()
            self.prior.unlink(missing_ok=True)
            print("CI_RUNTIME collection changed; replaying assigned tests", flush=True)
        self.collection = assigned
        groups = self.groups(assigned, self.settings.split_files)
        resumed = set().union(
            *(ids for ids in groups.values() if set(ids) <= self.restored)
        )
        selected = set(assigned) - resumed
        print(
            f"CI_RUNTIME shard={self.settings.shard + 1}/{self.settings.shards} "
            f"assigned={len(assigned)} resumed={len(resumed)} run={len(selected)}",
            flush=True,
        )
        return selected, resumed

    def record(self, nodeid: str, stage: str, passed: bool, seconds: float) -> None:
        with sqlite3.connect(self.db, timeout=SQLITE_TIMEOUT_SECONDS) as db:
            db.execute(
                "INSERT OR REPLACE INTO reports VALUES (?, ?, ?, ?)",
                (nodeid, stage, int(passed), seconds),
            )

    def snapshot(self) -> RuntimeManifest:
        with sqlite3.connect(self.db, timeout=SQLITE_TIMEOUT_SECONDS) as db:
            rows = db.execute(
                "SELECT nodeid, stage, passed, seconds FROM reports"
            ).fetchall()
        stages: dict[str, set[str]] = defaultdict(set)
        seconds = dict(self.prior_seconds)
        current: dict[str, float] = defaultdict(float)
        for nodeid, stage, passed, duration in rows:
            current[nodeid] += duration
            if passed:
                stages[nodeid].add(stage)
        seconds.update(current)
        passed_ids = {
            n for n, done in stages.items() if done == {"setup", "call", "teardown"}
        }
        groups = self.groups(self.collection, self.settings.split_files)
        complete = self.restored | passed_ids
        completed = set().union(
            *(ids for ids in groups.values() if set(ids) <= complete)
        )
        return RuntimeManifest(
            SCHEMA_VERSION,
            self.identity,
            self.collection,
            sorted(completed),
            {n: seconds[n] for n in self.collection if n in seconds},
            self.coverage_required,
            False,
        )

    def combine_coverage(self, destination: Path) -> bool:
        base = Path(os.environ.get("COVERAGE_FILE", ".coverage")).resolve()
        sources = [base, *sorted(base.parent.glob(base.name + ".*")), self.prior]
        merged = CoverageData(basename=str(destination))
        merged.erase()
        found = False
        for path in sources:
            if not path.is_file() or path.resolve() == destination.resolve():
                continue
            data = CoverageData(basename=str(path))
            data.read()
            if data.measured_files():
                merged.update(data)
                found = True
        if found:
            merged.write()
        return found

    def publish(self) -> None:
        with self.publish_lock:
            # Journal first, then coverage: each journal pass was recorded
            # only after the worker saved its coverage. The reverse read
            # order could snapshot an older trace beside newer pass records.
            manifest = self.snapshot()
            combined = self.state / "coverage.snapshot"
            manifest.coverage_file = self.combine_coverage(combined)
            if self.coverage_required and not manifest.coverage_file:
                return
            archive = self.state / "checkpoint.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                raw = json.dumps(asdict(manifest), sort_keys=True).encode()
                member = tarfile.TarInfo("manifest.json")
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
                if manifest.coverage_file:
                    tar.add(combined, arcname="coverage")
            self.store.upload(archive, self.checkpoint_key)

    def publish_quietly(self) -> None:
        try:
            self.publish()
        except (
            OSError,
            ValueError,
            CoverageException,
            sqlite3.DatabaseError,
            subprocess.SubprocessError,
        ) as exc:
            print(
                f"CI_RUNTIME checkpoint upload failed; next attempt may replay: {exc}",
                flush=True,
            )

    def start_uploader(self) -> None:
        self.uploader = threading.Thread(target=self._upload_loop, daemon=True)
        self.uploader.start()

    def _upload_loop(self) -> None:
        while not self.stop.wait(UPLOAD_INTERVAL_SECONDS):
            self.publish_quietly()

    def finish(self) -> None:
        self.stop.set()
        if self.uploader is not None:
            self.uploader.join()
        self.publish_quietly()
        manifest = self.snapshot()
        if manifest.completed:
            history = self.state / "history.json"
            history.write_text(
                json.dumps(
                    asdict(
                        RuntimeHistory(
                            SCHEMA_VERSION,
                            {
                                n: manifest.seconds[n]
                                for n in manifest.completed
                                if n in manifest.seconds
                            },
                        )
                    )
                ),
                encoding="utf-8",
            )
            digest = hashlib.sha256(history.read_bytes()).hexdigest()
            key = f"history/{self.settings.suite}/{self.settings.run}/{self.identity}-{digest}.json"
            try:
                self.store.upload(history, key)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"CI_RUNTIME timing upload failed: {exc}", flush=True)

    def restore_live_coverage(self) -> None:
        if not self.prior.is_file():
            return
        base = Path(os.environ.get("COVERAGE_FILE", ".coverage")).resolve()
        # pytest-cov's controller combines this alongside current worker data.
        data = CoverageData(basename=str(self.prior))
        data.read()
        prior_piece = CoverageData(basename=str(base) + ".ci_runtime_prior")
        prior_piece.update(data)
        prior_piece.write()
