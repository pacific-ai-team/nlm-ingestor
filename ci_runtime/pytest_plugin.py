"""Opt-in pytest hooks: ``-p ci_runtime.pytest_plugin`` with CI_RUNTIME_* set."""

from __future__ import annotations

import json
import os

import coverage
import pytest

from ci_runtime.config import RuntimeSettings
from ci_runtime.service import RuntimeService


class RuntimePlugin:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.worker = bool(os.environ.get("PYTEST_XDIST_WORKER"))
        self.service = RuntimeService(
            RuntimeSettings.from_environment(),
            json.dumps(config.invocation_params.args),
            bool(config.getoption("cov_source", default=[])),
        )
        self.total = 0
        self.resumed: set[str] = set()
        if not self.worker:
            self.service.load_history()
            self.service.initialise()
        else:
            payload = json.loads(
                (self.service.state / "worker-settings.json").read_text(
                    encoding="utf-8"
                )
            )
            self.service.history = payload["history"]
            self.service.restored = set(payload["restored"])
            self.service.collection = payload["collection"]
        if not self.worker:
            data = {
                "history": self.service.history,
                "restored": sorted(self.service.restored),
                "collection": self.service.collection,
            }
            (self.service.state / "worker-settings.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            metadata = dict(
                data,
                invocation=json.dumps(config.invocation_params.args),
                coverage_required=self.service.coverage_required,
                prior_seconds=self.service.prior_seconds,
            )
            (self.service.state / "invocation.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ):
        # After marker/diff filtering, including pytest's built-in -m/-k.
        yield
        self.total = len(items)
        selected, self.resumed = self.service.select([item.nodeid for item in items])
        kept = [item for item in items if item.nodeid in selected]
        dropped = [item for item in items if item.nodeid not in selected]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept
        plan = {
            "collection": self.service.collection,
            "total": self.total,
            "resumed": sorted(self.resumed),
        }
        tmp = self.service.state / (
            "collection-" + os.environ.get("PYTEST_XDIST_WORKER", "main") + ".json"
        )
        tmp.write_text(json.dumps(plan), encoding="utf-8")
        os.replace(tmp, self.service.state / "collection.json")
        if not self.worker:
            self.service.start_uploader()

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids: list[str]) -> None:
        del node, ids
        plan = json.loads(
            (self.service.state / "collection.json").read_text(encoding="utf-8")
        )
        self.service.collection = plan["collection"]
        self.total = plan["total"]
        self.resumed = set(plan["resumed"])
        if self.service.uploader is None:
            self.service.start_uploader()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        distributing = bool(self.config.getoption("numprocesses", default=0))
        if not self.worker and distributing:
            return
        if report.when == "teardown" and report.passed:
            current = coverage.Coverage.current()
            if current is not None:
                current.save()
        self.service.record(report.nodeid, report.when, report.passed, report.duration)

    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_runtestloop(self, session: pytest.Session):
        del session
        yield
        # pytest-cov combines in its runtestloop wrapper, before sessionfinish.
        # Exit our inner wrapper first so the retained trace joins its normal
        # report and fail-under calculation (not just the raw data afterward).
        if not self.worker:
            self.service.restore_live_coverage()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session):
        if self.worker:
            yield
            return
        if session.exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and self.total > 0:
            assigned = set(self.service.collection)
            if not assigned or assigned <= self.resumed:
                print(
                    f"CI_RUNTIME fully resumed or empty shard: {len(assigned)} assigned",
                    flush=True,
                )
                session.exitstatus = pytest.ExitCode.OK
                (self.service.state / "empty-ok").write_text(
                    "validated\n", encoding="utf-8"
                )
        self.service.finish()
        self.service.restore_live_coverage()
        yield


def pytest_configure(config: pytest.Config) -> None:
    if "CI_RUNTIME_STORE" in os.environ:
        config.pluginmanager.register(RuntimePlugin(config), "ci-runtime")
