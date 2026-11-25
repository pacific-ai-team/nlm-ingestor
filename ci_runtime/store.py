"""Small atomic objects in S3 (Argo) or a filesystem (tests/local runners)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from ci_runtime.config import STORAGE_TIMEOUT_SECONDS, RuntimeSettings
from ci_runtime.github_store import GitHubStore
from ci_runtime.types import StoredObject


class RuntimeStore:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        uri = urlsplit(settings.store)
        self.github = (
            GitHubStore(uri.netloc + uri.path) if uri.scheme == "github" else None
        )
        if uri.scheme not in ("s3", "file", "github"):
            raise ValueError("CI_RUNTIME_STORE must be s3://, file:// or github://")
        self.bucket = uri.netloc
        self.prefix = uri.path.strip("/")
        self.local = Path(uri.path) if uri.scheme == "file" else None
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if key.startswith("AWS_ENDPOINT_URL"):
                self.env.pop(key)
        if settings.credential_source == "irsa":
            for key in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE",
            ):
                self.env.pop(key, None)
        self.env["AWS_PAGER"] = ""

    def _aws(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["aws", *args],
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=STORAGE_TIMEOUT_SECONDS,
            check=check,
        )

    def objects(self, prefix: str) -> list[StoredObject]:
        if self.github is not None:
            return self.github.objects(prefix)
        if self.local is not None:
            base = self.local / prefix
            return (
                [
                    StoredObject(str(p.relative_to(self.local)), p.stat().st_mtime)
                    for p in sorted(base.rglob("*"))
                    if p.is_file()
                ]
                if base.exists()
                else []
            )
        full = f"{self.prefix}/{prefix}".lstrip("/")
        raw = json.loads(
            self._aws(
                "s3api",
                "list-objects-v2",
                "--bucket",
                self.bucket,
                "--prefix",
                full,
                "--output",
                "json",
            ).stdout
        )
        return [
            StoredObject(
                obj["Key"][len(self.prefix) + 1 :] if self.prefix else obj["Key"],
                datetime.fromisoformat(
                    obj["LastModified"].replace("Z", "+00:00")
                ).timestamp(),
            )
            for obj in raw.get("Contents", [])
        ]

    def download(self, key: str, dest: Path) -> bool:
        if self.github is not None:
            return self.github.download(key, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.local is not None:
            source = self.local / key
            if not source.is_file():
                return False
            shutil.copyfile(source, dest)
            return True
        result = self._aws(
            "s3",
            "cp",
            self.settings.store.rstrip("/") + "/" + key,
            str(dest),
            "--only-show-errors",
            check=False,
        )
        if result.returncode == 0:
            return True
        if "404" in result.stderr or "NoSuchKey" in result.stderr:
            return False
        raise OSError(f"CI storage read failed: {result.stderr.strip()}")

    def upload(self, source: Path, key: str) -> None:
        if self.github is not None:
            self.github.upload(source, key)
            return
        if self.local is not None:
            dest = self.local / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            temporary = dest.with_suffix(dest.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, dest)
        else:
            self._aws(
                "s3",
                "cp",
                str(source),
                self.settings.store.rstrip("/") + "/" + key,
                "--only-show-errors",
            )
