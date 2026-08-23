"""Tests for local project metadata consistency."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCALE = ROOT / "custom_components" / "unifi_presence" / "quality_scale.yaml"


def _parse_quality_scale_statuses() -> dict[str, dict[str, str]]:
    """Parse tiered rule statuses from the local quality scale file."""
    tiers: dict[str, dict[str, str]] = {"bronze": {}, "silver": {}, "gold": {}, "platinum": {}}
    text = QUALITY_SCALE.read_text()
    rules = yaml.safe_load(text)["rules"]
    current_tier = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            tier = stripped.removeprefix("#").strip().lower()
            if tier in tiers:
                current_tier = tier
            continue
        if current_tier and raw_line.startswith("  ") and not raw_line.startswith("    "):
            rule = stripped.split(":", maxsplit=1)[0]
            value = rules[rule]
            tiers[current_tier][rule] = value if isinstance(value, str) else value["status"]

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
    dev_dependencies = pyproject["dependency-groups"]["dev"]
    aiounifi_requirements = [dependency for dependency in dev_dependencies if dependency.startswith("aiounifi")]

    assert manifest["requirements"] == ["aiounifi==93"]
    assert aiounifi_requirements == ["aiounifi==93"]


def test_project_version_matches_between_manifest_and_pyproject() -> None:
    """Test the published project version is consistent across metadata files."""
    manifest = json.loads((ROOT / "custom_components" / "unifi_presence" / "manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert manifest["version"] == pyproject["project"]["version"]


def test_minimum_homeassistant_version_matches_between_hacs_and_readme() -> None:
    """Test the minimum supported Home Assistant version is consistent."""
    hacs = json.loads((ROOT / "hacs.json").read_text())
    readme = (ROOT / "README.md").read_text()

    assert f"Home Assistant {hacs['homeassistant']} or later" in readme
