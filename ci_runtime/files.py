"""Node, Vitest and pooled Jest execution using the durable file journal."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from ci_runtime.config import RuntimeSettings
from ci_runtime.service import RuntimeService


@dataclass(frozen=True)
class FileResult:
    success: bool
    recoverable: bool
    seconds: float


class FileRunner:
    def __init__(self, runner: str, state: Path, coverage: bool = False) -> None:
        self.runner = runner
        self.coverage = coverage
        self.state = state
        self.node = os.environ.get("CI_RUNTIME_TEST_NODE", "node")
        executable = shutil.which(self.node)
        if executable is None:
            raise ValueError("test Node executable is missing")
        self.node = str(Path(executable).resolve())
        self.environment = dict(os.environ)
        self.environment["PATH"] = (
            str(Path(self.node).parent) + os.pathsep + os.environ.get("PATH", "")
        )

    def collect(self, patterns: list[str]) -> list[str]:
        if self.runner == "jest":
            result = subprocess.run(
                [
                    self.node,
                    "node_modules/jest/bin/jest.js",
                    "--listTests",
                    "--json",
                    "--maxWorkers=1",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=self.environment,
            )
            paths = [Path(name) for name in json.loads(result.stdout)]
        elif self.runner == "vitest":
            result = subprocess.run(
                [
                    self.node,
                    "node_modules/vitest/vitest.mjs",
                    "list",
                    "--filesOnly",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=self.environment,
            )
            paths = [
                Path(item["file"] if isinstance(item, dict) else item)
                for item in json.loads(result.stdout)
            ]
        else:
            paths = [path for pattern in patterns for path in Path.cwd().glob(pattern)]
        root = Path.cwd().resolve()
        files = sorted({path.resolve().relative_to(root).as_posix() for path in paths})
        if not files or any(not Path(name).is_file() for name in files):
            raise ValueError("test discovery must return a nonempty set of files")
        return files

    def execute_jest_batch(self, files: list[str], jobs: int, record) -> list[bool]:
        if not files:
            return []
        config = self.jest_config()
        config["reporters"] = [
            [
                str(Path(__file__).with_name("jest_reporter.mjs")),
                {"state": str(self.state), "coverage": self.coverage},
            ]
        ]
        if self.coverage:
            config.update(collectCoverage=True, coverageReporters=[])
        args = [
            self.node,
            "node_modules/jest/bin/jest.js",
            "--ci",
            f"--maxWorkers={jobs}",
            "--config=" + json.dumps(config),
            "--runTestsByPath",
            *files,
        ]
        completed: set[str] = set()
        results: list[bool] = []
        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.environment,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                if not line.startswith("CI_RUNTIME_JEST_RESULT="):
                    print(line, end="", flush=True)
                    continue
                event = json.loads(line.split("=", 1)[1])
                filename = event.pop("filename")
                if filename not in files or filename in completed:
                    process.terminate()
                    raise ValueError("Jest reported an unexpected or duplicate file")
                completed.add(filename)
                result = FileResult(**event)
                print(
                    f"CI_RUNTIME file={filename} success={result.success} seconds={result.seconds:.2f}",
                    flush=True,
                )
                results.append(record(filename, result))
            code = process.wait()
        if completed != set(files) or code != 0:
            results.append(False)
        return results

    def execute(self, filename: str) -> FileResult:
        identifier = hashlib.sha256(filename.encode()).hexdigest()
        output = self.state / (identifier + ".log")
        report = self.state / (identifier + ".json")
        report.unlink(missing_ok=True)
        if self.runner == "node":
            args = [
                self.node,
                "--experimental-strip-types",
                "--test",
                "--test-reporter=tap",
                filename,
            ]
        elif self.runner == "jest":
            args = [
                self.node,
                "node_modules/jest/bin/jest.js",
                "--ci",
                "--runInBand",
                "--runTestsByPath",
                filename,
                "--json",
                "--outputFile=" + str(report),
            ]
            if self.coverage:
                config = self.jest_config()
                config["collectCoverageFrom"] = []
                config["coverageReporters"] = ["json"]
                config["coverageDirectory"] = str(self.coverage_file(filename).parent)
                args += ["--coverage", "--config=" + json.dumps(config)]
                self.coverage_file(filename).unlink(missing_ok=True)
        else:
            args = [
                self.node,
                "node_modules/vitest/vitest.mjs",
                "run",
                filename,
                "--reporter=json",
                "--outputFile=" + str(report),
                "--maxWorkers=1",
            ]
            metadata = Path("node_modules/vitest/package.json")
            if (
                metadata.is_file()
                and int(json.loads(metadata.read_text())["version"].split(".")[0]) < 4
            ):
                # Vitest2/3 otherwise inherit a CPU-based minimum larger than
                # this one-worker pool. Vitest4 removed the minimum option.
                args.append("--minWorkers=1")
        started = time.monotonic()
        with output.open("wb") as stream:
            result = subprocess.run(
                args,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
                env=self.environment,
            )
        seconds = time.monotonic() - started
        success = result.returncode == 0
        recoverable = False
        if success and self.runner == "node":
            text = output.read_text(encoding="utf-8", errors="replace")
            counts = {
                k: int(v)
                for k, v in re.findall(
                    r"^# (tests|fail|cancelled|skipped|todo) (\d+)\s*$",
                    text,
                    re.MULTILINE,
                )
            }
            names = re.findall(r"^\s*# Subtest: (.+)$", text, re.MULTILINE)
            # Node counts an empty file's process wrapper as one passing test.
            explicit = any(
                Path(name).resolve() != Path(filename).resolve() for name in names
            )
            success = (
                explicit
                and counts.get("tests", 0) > 0
                and counts.get("fail") == 0
                and counts.get("cancelled") == 0
            )
            recoverable = (
                success and counts.get("skipped") == 0 and counts.get("todo") == 0
            )
        elif success:
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
                actual = {Path(item["name"]).resolve() for item in data["testResults"]}
                success = (
                    data["success"] is True
                    and data["numTotalTests"] > 0
                    and data["numFailedTests"] == 0
                    and actual == {Path(filename).resolve()}
                )
                recoverable = (
                    success
                    and data["numPendingTests"] == 0
                    and data.get("numTodoTests", 0) == 0
                )
            except (KeyError, ValueError, OSError, TypeError):
                success = False
        print(
            f"CI_RUNTIME file={filename} success={success} seconds={seconds:.2f}",
            flush=True,
        )
        if not success:
            # Keep passing-suite output on disk; print failures as ordinary runner logs.
            with output.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    print(line, end="", flush=True)
        return FileResult(success, recoverable, seconds)

    def run(self, settings: RuntimeSettings, patterns: list[str], jobs: int) -> int:
        node_version = subprocess.check_output(
            [self.node, "--version"], text=True
        ).strip()
        invocation = json.dumps(
            ["files", self.runner, sorted(patterns), jobs, node_version, self.coverage]
        )
        service = RuntimeService(settings, invocation, False)
        service.load_history()
        service.initialise()
        prior_coverage = self.state / "prior-istanbul.json"
        prior_coverage.unlink(missing_ok=True)
        coverage_inputs: list[Path] = []
        coverage_lock = Lock()
        if self.coverage and service.restored:
            archive = self.state / "prior-istanbul.json.gz"
            if service.store.download(self.coverage_key(service), archive):
                prior_coverage.write_bytes(gzip.decompress(archive.read_bytes()))
                self.validate_coverage(prior_coverage)
                coverage_inputs.append(prior_coverage)
            else:
                service.restored.clear()
        selected, restored = service.select(self.collect(patterns))
        if self.coverage and not restored:
            coverage_inputs.clear()
        service.start_uploader()

        def execute(filename: str, result: FileResult | None = None) -> bool:
            result = result or self.execute(filename)

            def record() -> None:
                service.record(filename, "setup", result.recoverable, 0)
                service.record(filename, "call", result.recoverable, result.seconds)
                service.record(filename, "teardown", result.recoverable, 0)

            if self.coverage and result.success:
                with coverage_lock:
                    data = self.coverage_file(filename)
                    self.validate_coverage(data)
                    if json.loads(data.read_text()) or not coverage_inputs:
                        merged = self.state / "coverage-final.json"
                        self.merge_coverage([*coverage_inputs, data], merged)
                        coverage_inputs[:] = [merged]
                        archive = self.state / "istanbul.json.gz"
                        archive.write_bytes(
                            gzip.compress(merged.read_bytes(), compresslevel=1)
                        )
                        # A coverage snapshot reaches storage before any completed
                        # file can enter an uploaded journal checkpoint. A file
                        # importing no product code reuses the existing snapshot.
                        service.store.upload(archive, self.coverage_key(service))
                    record()
            else:
                record()
            return result.success

        try:
            ordered = sorted(selected, key=lambda n: (-service.history.get(n, 0), n))
            if self.runner == "jest":
                results = self.execute_jest_batch(ordered, jobs, execute)
            else:
                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    results = list(pool.map(execute, ordered))
            if self.coverage and all(results):
                self.merge_coverage(
                    coverage_inputs,
                    self.state / "coverage-final.json",
                )
            return 0 if all(results) else 1
        finally:
            service.finish()

    def jest_config(self) -> dict:
        result = subprocess.check_output(
            [
                self.node,
                "-e",
                "console.log(JSON.stringify(require('./jest.config.js')))",
            ],
            text=True,
            env=self.environment,
        )
        return {**json.loads(result), "rootDir": str(Path.cwd())}

    def coverage_file(self, filename: str) -> Path:
        identifier = hashlib.sha256(filename.encode()).hexdigest()
        return self.state / "istanbul" / identifier / "coverage-final.json"

    @staticmethod
    def coverage_key(service: RuntimeService) -> str:
        return f"istanbul/{service.settings.suite}/{service.settings.run}/{service.identity}/coverage.json.gz"

    @staticmethod
    def validate_coverage(filename: Path) -> None:
        data = json.loads(filename.read_text())
        if not isinstance(data, dict) or any(
            not isinstance(value, dict)
            or not {"path", "statementMap", "fnMap", "branchMap", "s", "f", "b"}
            <= value.keys()
            for value in data.values()
        ):
            raise ValueError("invalid Istanbul coverage")

    def merge_coverage(
        self,
        files: list[Path],
        output: Path,
        reports: bool = False,
        remap: bool = False,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Resolve through Jest: these are its own declared Istanbul dependencies.
        script = """
void (async () => {
const fs = require('node:fs');
const path = require('node:path');
const from = require.resolve('@jest/reporters');
const load = name => require(require.resolve(name, {paths: [path.dirname(from)]}));
let map = load('istanbul-lib-coverage').createCoverageMap({});
const [output, reports, remap, ...inputs] = process.argv.slice(1);
for (const input of inputs) map.merge(JSON.parse(fs.readFileSync(input, 'utf8')));
if (remap === 'yes') map = await load('istanbul-lib-source-maps').createSourceMapStore().transformCoverage(map);
fs.writeFileSync(output, JSON.stringify(map.toJSON()));
if (reports === 'yes') {
  const context = load('istanbul-lib-report').createContext({dir: path.dirname(output), coverageMap: map});
  for (const format of ['text-summary', 'cobertura']) load('istanbul-reports').create(format).execute(context);
}
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(
            [
                self.node,
                "-e",
                script,
                str(output),
                "yes" if reports else "no",
                "yes" if remap else "no",
                *map(str, files),
            ],
            check=True,
            env=self.environment,
        )

    def coverage_inventory(self) -> Path:
        # Run no product tests here: one empty harness asks Jest to inventory the
        # original collectCoverageFrom surface, including never-imported files.
        harness = self.state / "coverage-inventory.test.js"
        harness.write_text("test('coverage inventory', () => {});\n")
        config = self.jest_config()
        config.update(
            testMatch=[str(harness)],
            testPathIgnorePatterns=["/node_modules/"],
            coverageDirectory=str(self.state / "inventory"),
            coverageReporters=["json"],
        )
        try:
            subprocess.run(
                [
                    self.node,
                    "node_modules/jest/bin/jest.js",
                    "--ci",
                    "--runInBand",
                    "--runTestsByPath",
                    str(harness),
                    "--coverage",
                    "--config=" + json.dumps(config),
                ],
                check=True,
                env=self.environment,
            )
        finally:
            harness.unlink(missing_ok=True)
        dest = self.state / "inventory" / "coverage-final.json"
        self.validate_coverage(dest)
        if not json.loads(dest.read_text()):
            raise ValueError("whole-product coverage inventory is empty")
        return dest


def main(args: list[str]) -> int:
    if args[:1] == ["merge-jest-coverage"]:
        parser = argparse.ArgumentParser(
            description="Merge shard coverage with the complete product inventory"
        )
        parser.add_argument("--input", action="append", required=True)
        parser.add_argument(
            "--output", type=Path, default=Path("coverage/coverage-final.json")
        )
        options = parser.parse_args(args[1:])
        runner = FileRunner("jest", Path(".ci-runtime/coverage-merge").resolve(), True)
        runner.state.mkdir(parents=True, exist_ok=True)
        inputs = [Path(name).resolve() for name in options.input]
        for filename in inputs:
            runner.validate_coverage(filename)
        remapped = runner.state / "remapped-shards.json"
        runner.merge_coverage(inputs, remapped, remap=True)
        runner.merge_coverage(
            [runner.coverage_inventory(), remapped], options.output.resolve(), True
        )
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", choices=["node", "vitest", "jest"], required=True)
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--coverage", action="store_true")
    options = parser.parse_args(args)
    if options.jobs < 1 or (options.runner == "node" and not options.pattern):
        parser.error("positive jobs and explicit Node test patterns are required")
    if options.coverage and options.runner != "jest":
        parser.error("file coverage is currently supported for Jest only")
    settings = RuntimeSettings.from_environment()
    settings.state.mkdir(parents=True, exist_ok=True)
    return FileRunner(options.runner, settings.state.resolve(), options.coverage).run(
        settings, options.pattern, options.jobs
    )
