"""Tests for local project metadata consistency."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCALE = ROOT / "custom_components" / "unifi_presence" / "quality_scale.yaml"


def _parse_quality_scale_statuses() -> dict[str, dict[str, str]]:
    """Parse tiered rule statuses from the local quality scale file."""
    tiers: dict[str, dict[str, str]] = {
        "bronze": {},
        "silver": {},
        "gold": {},
        "platinum": {},
    }
    current_tier = ""
    pending_rule = ""

    for raw_line in QUALITY_SCALE.read_text().splitlines():
        stripped = raw_line.strip()
        if stripped == "# Bronze":
            current_tier = "bronze"
            pending_rule = ""
            continue
        if stripped == "# Silver":
            current_tier = "silver"
            pending_rule = ""
            continue
        if stripped == "# Gold":
            current_tier = "gold"
            pending_rule = ""
            continue
        if stripped == "# Platinum":
            current_tier = "platinum"
            pending_rule = ""
            continue
        if not current_tier or not stripped or stripped == "rules:":
            continue
        if raw_line.startswith("  ") and not raw_line.startswith("    ") and stripped.endswith(":"):
            pending_rule = stripped.removesuffix(":")
            continue
        if raw_line.startswith("  ") and not raw_line.startswith("    ") and ": " in stripped:
            rule, status = stripped.split(": ", maxsplit=1)
            tiers[current_tier][rule] = status
            pending_rule = ""
            continue
        if pending_rule and stripped.startswith("status: "):
            tiers[current_tier][pending_rule] = stripped.removeprefix("status: ")
            pending_rule = ""

    return tiers


def test_manifest_quality_scale_matches_quality_scale_yaml() -> None:
    """Test the manifest quality scale matches the local quality scale file."""
    manifest = json.loads((ROOT / "custom_components" / "unifi_presence" / "manifest.json").read_text())
    quality_scale = _parse_quality_scale_statuses()

    assert quality_scale["bronze"]
    assert quality_scale["silver"]
    assert all(status in {"done", "exempt"} for status in quality_scale["bronze"].values())
    assert all(status in {"done", "exempt"} for status in quality_scale["silver"].values())
    assert quality_scale["gold"]["discovery"] == "not-done"
    assert manifest["quality_scale"] == "silver"


def test_aiounifi_version_matches_between_manifest_and_pyproject() -> None:
    """Test aiounifi is pinned consistently in manifest and pyproject."""
    manifest = json.loads((ROOT / "custom_components" / "unifi_presence" / "manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    aiounifi_requirements = [dependency for dependency in dev_dependencies if dependency.startswith("aiounifi")]

    assert manifest["requirements"] == ["aiounifi==90"]
    assert aiounifi_requirements == ["aiounifi==90"]


def test_project_version_matches_between_manifest_and_pyproject() -> None:
    """Test the published project version is consistent across metadata files."""
    manifest = json.loads((ROOT / "custom_components" / "unifi_presence" / "manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert manifest["version"] == pyproject["project"]["version"]


def test_hacs_homeassistant_version_matches_readme_requirements() -> None:
    """Test HACS minimum HA version matches the README requirement."""
    hacs = json.loads((ROOT / "hacs.json").read_text())
    readme = (ROOT / "README.md").read_text()

    assert f"- Home Assistant {hacs['homeassistant']} or later" in readme


def test_quality_commands_and_coverage_are_consistent_in_readme() -> None:
    """Test README dev commands match the canonical pyproject thresholds."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    readme = (ROOT / "README.md").read_text()

    coverage = pyproject["tool"]["pytest"]["ini_options"]["addopts"][2].removeprefix("--cov-fail-under=")

    assert 'pip install ".[dev]"' in readme
    assert "pre-commit install" in readme
    assert "PYTHONPATH=. pytest tests/ -v" in readme
    assert "ruff check ." in readme
    assert "ruff format --check ." in readme
    assert "mypy --strict custom_components/unifi_presence/" in readme
    assert f"Coverage is enforced at {coverage}% minimum" in readme


def test_validate_workflow_uses_stable_action_refs() -> None:
    """Test CI workflow avoids floating branch refs for third-party actions."""
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text()

    assert "hacs/action@22.5.0" in workflow
    assert "home-assistant/actions/hassfest@1.0.0" in workflow
    assert "@main" not in workflow
    assert "@master" not in workflow
