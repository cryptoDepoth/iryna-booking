"""Regression coverage for test-only browser dependency portability."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _declared_requirements(filename):
    return {
        line.strip()
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_browser_regression_dependencies_are_declared_and_pinned():
    assert _declared_requirements("requirements-test.txt") == {
        "-r requirements.txt",
        "playwright==1.60.0",
        "pytest==9.0.3",
    }


def test_browser_tooling_stays_out_of_production_runtime():
    runtime_requirements = _declared_requirements("requirements.txt")
    assert not any(
        dependency.startswith(("playwright", "pytest"))
        for dependency in runtime_requirements
    )

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").lower()
    assert "requirements-test.txt" not in dockerfile
    assert "playwright" not in dockerfile
    assert "chromium" not in dockerfile
