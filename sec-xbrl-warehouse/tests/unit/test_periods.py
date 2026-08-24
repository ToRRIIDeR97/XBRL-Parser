from datetime import date

import pytest

from sec_xbrl_warehouse.domain.enums import PeriodKind
from sec_xbrl_warehouse.normalization.periods import infer_quarter_count, normalize_period


def test_duration_period_hint() -> None:
    period = normalize_period(
        kind=PeriodKind.DURATION,
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert period.duration_days == 365
    assert period.quarter_count_hint == 4
    assert period.classification == "annual"


def test_instant_validation() -> None:
    period = normalize_period(kind=PeriodKind.INSTANT, instant=date(2025, 12, 31))
    assert period.classification == "instant"
    with pytest.raises(ValueError):
        normalize_period(kind=PeriodKind.INSTANT, start=date(2025, 1, 1))


def test_nonstandard_duration() -> None:
    assert infer_quarter_count(17) is None
