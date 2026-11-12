"""The requirement files must agree with each other, and every locked package needs a checksum.

These catch the easy mistakes: editing a version without regenerating the lock
files, or regenerating one without ``--generate-hashes``. See "Changing
dependencies" in the README.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s;\\]+)")


def _name(raw: str) -> str:
    """Package names compare case-insensitively, with ``.``, ``_`` and ``-`` treated alike."""
    return re.sub(r"[._-]+", "-", raw).lower()


def direct_pins(filename: str) -> dict[str, str]:
    """``name -> version`` for the hand-edited files (requirements.txt, requirements-dev.txt)."""
    pins = {}
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        if match := PIN.match(line.strip()):
            pins[_name(match.group(1))] = match.group(2)
    return pins


def locked(filename: str) -> dict[str, tuple[str, int]]:
    """``name -> (version, number of checksums)`` for a generated lock file."""
    text = (ROOT / filename).read_text(encoding="utf-8").replace("\\\n", " ")  # join continued lines
    entries = {}
    for line in text.splitlines():
        if match := PIN.match(line.strip()):
            entries[_name(match.group(1))] = (match.group(2), line.count("--hash=sha256:"))
    return entries


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-dev.lock"])
def test_every_locked_package_has_a_checksum(lock):
    entries = locked(lock)
    assert len(entries) > 30  # the parser found the packages at all
    assert [name for name, (_, hashes) in entries.items() if hashes == 0] == []


def test_runtime_lock_matches_requirements_txt():
    lock = locked("requirements.lock")
    for name, version in direct_pins("requirements.txt").items():
        assert lock.get(name, ("missing",))[0] == version, f"{name}: regenerate requirements.lock"


def test_dev_lock_matches_requirements_dev_txt():
    lock = locked("requirements-dev.lock")
    dev_pins = direct_pins("requirements-dev.txt")
    assert {"pytest", "ruff"} <= set(dev_pins)
    for name, version in dev_pins.items():
        assert lock.get(name, ("missing",))[0] == version, f"{name}: regenerate requirements-dev.lock"


def test_tests_run_against_the_same_versions_that_run_in_production():
    runtime, dev = locked("requirements.lock"), locked("requirements-dev.lock")
    different = {name: (version, dev.get(name, ("missing",))[0]) for name, (version, _) in runtime.items()}
    assert {name: pair for name, pair in different.items() if pair[0] != pair[1]} == {}


def test_production_and_ci_installs_insist_on_checksums():
    assert "pip install --require-hashes -r requirements.lock" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install --require-hashes -r requirements-dev.lock" in ci
