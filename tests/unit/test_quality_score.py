"""Unit tests for QualityScore — M4-1 (strict TDD).

QualityScore is a Pydantic v2 model in borgesica.domain.models.
All four fields (accuracy, fluency, neutral_register, glossary_consistency)
are required integers constrained to [1, 5].
"""
import pytest
from pydantic import ValidationError

from borgesica.domain.models import QualityScore


class TestQualityScoreValidRange:
    """All four fields accept every value in [1, 5]."""

    def test_minimum_values(self):
        qs = QualityScore(
            accuracy=1,
            fluency=1,
            neutral_register=1,
            glossary_consistency=1,
        )
        assert qs.accuracy == 1
        assert qs.fluency == 1
        assert qs.neutral_register == 1
        assert qs.glossary_consistency == 1

    def test_maximum_values(self):
        qs = QualityScore(
            accuracy=5,
            fluency=5,
            neutral_register=5,
            glossary_consistency=5,
        )
        assert qs.accuracy == 5
        assert qs.fluency == 5
        assert qs.neutral_register == 5
        assert qs.glossary_consistency == 5

    def test_mid_values(self):
        qs = QualityScore(
            accuracy=3,
            fluency=4,
            neutral_register=2,
            glossary_consistency=5,
        )
        assert qs.accuracy == 3
        assert qs.fluency == 4
        assert qs.neutral_register == 2
        assert qs.glossary_consistency == 5


class TestQualityScoreOutOfRange:
    """Out-of-range values (0 or 6) raise ValidationError on each field."""

    @pytest.mark.parametrize("field,value", [
        ("accuracy", 0),
        ("accuracy", 6),
        ("fluency", 0),
        ("fluency", 6),
        ("neutral_register", 0),
        ("neutral_register", 6),
        ("glossary_consistency", 0),
        ("glossary_consistency", 6),
    ])
    def test_out_of_range_raises(self, field: str, value: int):
        data = {
            "accuracy": 3,
            "fluency": 3,
            "neutral_register": 3,
            "glossary_consistency": 3,
        }
        data[field] = value
        with pytest.raises(ValidationError):
            QualityScore(**data)


class TestQualityScoreRequiredFields:
    """All four fields are required — omitting any one raises ValidationError."""

    @pytest.mark.parametrize("missing_field", [
        "accuracy",
        "fluency",
        "neutral_register",
        "glossary_consistency",
    ])
    def test_missing_field_raises(self, missing_field: str):
        data = {
            "accuracy": 3,
            "fluency": 3,
            "neutral_register": 3,
            "glossary_consistency": 3,
        }
        del data[missing_field]
        with pytest.raises(ValidationError):
            QualityScore(**data)
