"""Regression coverage for test-only browser dependency portability."""

from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME_SEPARATORS = re.compile(r"[-_.]+")
PACKAGE_NAME_PREFIX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _declared_requirements(filename):
    return {
        line.strip()
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _included_requirement(line):
    tokens = shlex.split(line, comments=True)
    if not tokens:
        return None

    option = tokens[0]
    if option in {"-r", "--requirement"}:
        return tokens[1] if len(tokens) > 1 else None
    if option.startswith("--requirement="):
        return option.split("=", 1)[1]
    if option.startswith("-r") and option != "-r":
        return option[2:]
    return None


def _normalized_requirement_name(line):
    match = PACKAGE_NAME_PREFIX.match(line.strip())
    if not match:
        return None
    return PACKAGE_NAME_SEPARATORS.sub("-", match.group(0)).lower()


def _resolved_requirement_names(requirements_path, visited=None):
    requirements_path = Path(requirements_path).resolve()
    visited = set() if visited is None else visited
    if requirements_path in visited:
        return set()
    visited.add(requirements_path)

    package_names = set()
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        included_requirement = _included_requirement(stripped)
        if included_requirement:
            package_names.update(
                _resolved_requirement_names(
                    requirements_path.parent / included_requirement,
                    visited,
                )
            )
            continue

        package_name = _normalized_requirement_name(stripped)
        if package_name:
            package_names.add(package_name)

    return package_names


def test_browser_regression_dependencies_are_declared_and_pinned():
    assert _declared_requirements("requirements-test.txt") == {
        "-r requirements.txt",
        "playwright==1.60.0",
        "pytest==9.0.3",
    }


def test_resolved_requirement_names_normalize_and_follow_nested_includes(tmp_path):
    requirements = tmp_path / "requirements.txt"
    included_dir = tmp_path / "config"
    nested_dir = included_dir / "nested"
    nested_dir.mkdir(parents=True)

    requirements.write_text(
        "--requirement config/base.txt\n",
        encoding="utf-8",
    )
    (included_dir / "base.txt").write_text(
        "-r nested/browser.txt\nExample_Package.Name==1.0\n",
        encoding="utf-8",
    )
    (nested_dir / "browser.txt").write_text(
        "PlayWright==1.60.0\nPyTeSt==9.0.3\n",
        encoding="utf-8",
    )

    assert _resolved_requirement_names(requirements) == {
        "example-package-name",
        "playwright",
        "pytest",
    }


def test_browser_tooling_stays_out_of_production_runtime():
    runtime_requirement_names = _resolved_requirement_names(ROOT / "requirements.txt")
    assert {"playwright", "pytest"}.isdisjoint(runtime_requirement_names)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").lower()
    assert "requirements-test.txt" not in dockerfile
    assert "playwright" not in dockerfile
    assert "chromium" not in dockerfile
