"""Pytest configuration: auto-creates test_result/ and writes per-module reports."""

from collections import defaultdict
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.parent / "test_result"


def pytest_configure(config):
    RESULTS_DIR.mkdir(exist_ok=True)


class _ModuleReporter:
    def __init__(self):
        self._reports: dict[str, list] = defaultdict(list)
        self._start: dict[str, datetime] = {}

    def record(self, report):
        module = report.nodeid.split("::")[0]
        if module not in self._start:
            self._start[module] = datetime.now()
        self._reports[module].append(report)

    def flush(self):
        for module, reports in self._reports.items():
            ts = self._start[module].strftime("%Y%m%d_%H%M%S")
            stem = Path(module).stem
            out = RESULTS_DIR / f"{stem}_{ts}.txt"

            passed = failed = errored = 0
            lines = [
                f"Module : {module}",
                f"Started: {self._start[module].isoformat(timespec='seconds')}",
                "",
            ]

            for r in reports:
                if r.when == "call" or (r.when in ("setup", "teardown") and not r.passed):
                    is_xfail = hasattr(r, "wasxfail")
                    if r.passed and not is_xfail:
                        status = "PASSED"
                        passed += 1
                    elif r.failed and is_xfail:
                        # xfail(strict=True) unexpectedly passed → counts as failure
                        status = "XPASS"
                        failed += 1
                    elif r.failed:
                        status = "FAILED"
                        failed += 1
                    elif is_xfail:
                        status = "XFAIL"
                        passed += 1
                    else:
                        status = "ERROR"
                        errored += 1

                    lines.append(f"[{status}] {r.nodeid}")
                    if not r.passed and r.longrepr and status not in ("XFAIL",):
                        lines.append(str(r.longrepr))
                    lines.append("")

            xfailed = sum(
                1 for r in reports
                if r.when == "call" and hasattr(r, "wasxfail") and not r.failed
            )
            lines.append(
                f"Summary: {passed} passed, {failed} failed, {errored} errors, {xfailed} xfailed"
            )
            out.write_text("\n".join(lines), encoding="utf-8")


_reporter = _ModuleReporter()


def pytest_runtest_logreport(report):
    _reporter.record(report)


def pytest_sessionfinish(session, exitstatus):
    _reporter.flush()
