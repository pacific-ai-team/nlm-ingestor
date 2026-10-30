"""Append-only GitHub Actions artifacts, via the official Node SDK."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ci_runtime.config import STORAGE_TIMEOUT_SECONDS
from ci_runtime.types import StoredObject


class GitHubStore:
    def __init__(self, repository: str) -> None:
        if repository != os.environ.get("GITHUB_REPOSITORY"):
            raise ValueError("artifact storage must belong to the current repository")
        for key in ("CI_RUNTIME_GITHUB_TOKEN", "CI_RUNTIME_ARTIFACT_SDK"):
            if not os.environ.get(key):
                raise ValueError(f"{key} is required for GitHub artifact storage")

    def call(self, operation: str, key: str, filename: Path | None = None) -> str:
        args = [
            "node",
            str(Path(__file__).with_name("github_artifacts.mjs")),
            operation,
            key,
        ]
        if filename is not None:
            args.append(str(filename.resolve()))
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=STORAGE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode:
            raise OSError("CI artifact operation failed")
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("CI_RUNTIME_JSON="):
                return line.removeprefix("CI_RUNTIME_JSON=")
        raise OSError("CI artifact helper returned no result")

    def objects(self, prefix: str) -> list[StoredObject]:
        return [
            StoredObject(**item) for item in json.loads(self.call("objects", prefix))
        ]

    def download(self, key: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return bool(json.loads(self.call("download", key, dest)))

    def upload(self, source: Path, key: str) -> None:
        self.call("upload", key, source)
