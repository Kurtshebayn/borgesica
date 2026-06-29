"""Guard test for golden fixtures — M4-1.

Ensures every YAML fixture file in tests/golden/ loads successfully and
carries the four required fields: source, expected, glossary, notes.

This is a deterministic Tier-1 guard — no live LLM required.
The LLM-as-judge harness (M4-2) builds on top of these fixtures.
"""
from __future__ import annotations

import pathlib

import yaml

GOLDEN_DIR = pathlib.Path(__file__).parent.parent / "golden"
REQUIRED_FIELDS = {"source", "expected", "glossary", "notes"}


def _fixture_files() -> list[pathlib.Path]:
    return sorted(GOLDEN_DIR.glob("*.yaml"))


def test_golden_directory_has_minimum_fixtures():
    """At least 9 fixture files must exist (5 SRT + 3 prose + 1 calque)."""
    files = _fixture_files()
    assert len(files) >= 9, (
        f"Expected ≥ 9 golden fixtures, found {len(files)}: {[f.name for f in files]}"
    )


class TestEachFixtureLoadsAndHasRequiredFields:
    """Parametrize over every .yaml file in tests/golden/."""

    def _load(self, path: pathlib.Path) -> dict:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_all_fixtures_parseable_and_complete(self):
        """Every fixture YAML must parse and contain all four required keys."""
        files = _fixture_files()
        assert files, "No .yaml files found in tests/golden/"
        errors: list[str] = []
        for path in files:
            try:
                data = self._load(path)
            except yaml.YAMLError as exc:
                errors.append(f"{path.name}: YAML parse error — {exc}")
                continue
            missing = REQUIRED_FIELDS - set(data.keys())
            if missing:
                errors.append(f"{path.name}: missing fields {sorted(missing)}")
        assert not errors, "Golden fixture schema violations:\n" + "\n".join(errors)

    def test_glossary_field_is_a_list(self):
        """The `glossary` field in each fixture must be a list (possibly empty)."""
        files = _fixture_files()
        errors: list[str] = []
        for path in files:
            data = self._load(path)
            if "glossary" not in data:
                continue  # caught by the completeness test above
            if not isinstance(data["glossary"], list):
                errors.append(
                    f"{path.name}: `glossary` must be a list, got {type(data['glossary']).__name__}"
                )
        assert not errors, "\n".join(errors)

    def test_source_and_expected_are_non_empty_strings(self):
        """Both `source` and `expected` must be non-empty strings."""
        files = _fixture_files()
        errors: list[str] = []
        for path in files:
            data = self._load(path)
            for field in ("source", "expected"):
                val = data.get(field)
                if not isinstance(val, str) or not val.strip():
                    errors.append(
                        f"{path.name}: `{field}` must be a non-empty string, got {val!r}"
                    )
        assert not errors, "\n".join(errors)
