"""Flush a live attempt's checkpoint from an Argo TERM trap."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from ci_runtime.config import SCHEMA_VERSION, RuntimeSettings
from ci_runtime.coverage_artifacts import CoverageArtifacts
from ci_runtime.service import RuntimeService
from ci_runtime.types import RuntimePlan


def main() -> None:
    if sys.argv[1:2] == ["files"]:
        from ci_runtime.files import main as files_main

        raise SystemExit(files_main(sys.argv[2:]))
    settings = RuntimeSettings.from_environment()
    if sys.argv[1:] == ["history-plan"]:
        os.environ["CI_RUNTIME_BUILDING_PLAN"] = "1"
        service = RuntimeService(settings, "history-plan", False)
        service.load_history()
        payload = RuntimePlan(
            SCHEMA_VERSION,
            settings.suite,
            settings.revision,
            settings.run,
            settings.shards,
            settings.cutoff,
            service.history,
        )
        (settings.state / "history-plan.json").write_text(
            json.dumps(asdict(payload)), encoding="utf-8"
        )
        return
    if sys.argv[1:] == ["coverage-upload"]:
        CoverageArtifacts(settings).publish()
        return
    if sys.argv[1:] == ["coverage-merge"]:
        CoverageArtifacts(settings).merge(Path(".coverage"))
        return
    if sys.argv[1:] != ["flush"]:
        raise SystemExit(
            "usage: python -m ci_runtime {files|history-plan|flush|coverage-upload|coverage-merge}"
        )
    meta_path = settings.state / "invocation.json"
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    service = RuntimeService(settings, meta["invocation"], meta["coverage_required"])
    service.restored = set(meta["restored"])
    service.prior_seconds = meta["prior_seconds"]
    plan_path = settings.state / "collection.json"
    if plan_path.is_file():
        service.collection = json.loads(plan_path.read_text(encoding="utf-8"))[
            "collection"
        ]
    else:
        service.collection = meta["collection"]
    service.publish_quietly()


if __name__ == "__main__":
    main()
