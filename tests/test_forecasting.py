"""Tests for the Holt-Winters cost forecaster (finops.ml.forecasting).

Bug: forecast_costs returned an exact $0.00, with a zero-width interval, on
every 7th day of the horizon on a live account steadily spending $600-700/day.
Root cause: the last input day is often not-yet-fully-posted (billing/usage
records lag), reads near-zero, and Holt-Winters bakes that into the seasonal
slot for that weekday. Because the seasonal array repeats every SEASON_LEN (7)
entries, the corrupted slot resurfaces on every 7th forecast day, and the
resulting deeply negative point estimate gets clamped to an exact 0 alongside
a lower/upper interval that clamps to 0 too.
"""
from __future__ import annotations

import pytest

import finops.ml.forecasting as forecasting
from finops.ml.forecasting import Forecaster

# ~$650/day average with a repeating weekly wobble (Mon..Sun), shaped like the
# real account from the dogfood: steady, not flat, not trending.
_WEEKLY_PATTERN = [620.0, 645.0, 660.0, 700.0, 685.0, 630.0, 610.0]


def _steady_series(n_days: int) -> list[float]:
    """`n_days` of a steady ~$650/day account with weekly wobble, oldest first."""
    return [_WEEKLY_PATTERN[i % 7] for i in range(n_days)]


@pytest.fixture(autouse=True)
def _no_model_persistence(monkeypatch):
    """fit() persists tuned params to the DB on every Holt-Winters fit. These
    tests exercise fit() directly with an in-memory series and have no DB to
    write to (and shouldn't touch the developer's real one), so stub the save
    the same way the class already tolerates a save failure (log + continue)."""
    monkeypatch.setattr(forecasting, "_save_model", lambda *a, **k: None)


# ── The bug, end to end ───────────────────────────────────────────────────────

def test_forecast_has_no_exact_zero_day_after_partial_trailing_day():
    series = _steady_series(90)
    trailing_30d_spend = sum(series[-30:])
    series[-1] = 15.0  # last day not yet fully posted: reads near-zero

    f = Forecaster(account_id="111122223333").fit(series)
    result = f.predict(horizon_days=30)

    assert result.method == "holt_winters"
    assert min(result.point) > 300, (
        f"a forecast day drops to near/exact zero: {result.point}")

    # The reported symptom precisely: every 7th day (same weekday as the
    # corrupted trailing day) was an exact $0.00 with lower == upper == 0.
    for h in (7, 14, 21, 28):
        pt = result.point[h - 1]
        assert pt > 300, f"day {h} of the horizon is ${pt}, the every-7th-day zero bug"
        assert not (result.lower[h - 1] == 0 and result.upper[h - 1] == 0), (
            f"day {h} has a zero-width interval collapsed to 0: "
            f"lower={result.lower[h-1]} upper={result.upper[h-1]}")

    # The monthly projection should sit near real trailing spend, not be
    # dragged to ~half of it by the four-plus corrupted days in the horizon.
    assert 0.7 * trailing_30d_spend <= result.monthly_projection <= 1.3 * trailing_30d_spend, (
        f"monthly projection ${result.monthly_projection} vs trailing spend "
        f"${trailing_30d_spend}")


def test_forecast_dict_shape_has_no_zero_point_via_public_predict_dict():
    """Same scenario through the exact call the forecast_costs tool makes."""
    series = _steady_series(90)
    series[-1] = 8.0

    f = Forecaster(account_id="111122223333").fit(series)
    out = f.predict_dict(horizon_days=30)

    points = [day["point"] for day in out["forecast"]]
    assert all(p > 300 for p in points), f"a $0.00 (or near) day leaked through: {points}"
    assert out["monthly_projection"] > 10_000, (
        "monthly projection collapsed toward half of real trailing spend: "
        f"{out['monthly_projection']}")


# ── The guard itself, isolated ────────────────────────────────────────────────

def test_clean_trailing_partial_day_replaces_only_the_last_point():
    # 21 days (3 full weeks), not 14: the detector now compares series[-1] to
    # its own prior same-weekday observations and needs >= 2 of them before it
    # will trust the comparison (see _MIN_SAME_WEEKDAY_SAMPLES). 14 days only
    # gives one prior occurrence of the trailing day's weekday, which used to
    # be enough for the old flat-trailing-median check but is deliberately
    # insufficient now, so the series is long enough to exercise the override.
    series = _steady_series(21)
    series[-1] = 10.0  # ~1.5% of the weekly pattern's typical value

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned[:-1] == series[:-1]
    assert cleaned[-1] != 10.0
    assert cleaned[-1] > 300


def test_clean_trailing_partial_day_leaves_normal_wobble_alone():
    """The lowest day in the normal weekly pattern (Sunday, $610) must not be
    treated as a partial-day artifact just for being the smallest value."""
    series = _steady_series(14)  # series[-1] is a normal $610 Sunday

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


def test_clean_trailing_partial_day_does_not_mask_a_genuinely_idle_account():
    """Do not silently mask real zeros: an account that is genuinely near-zero
    all week (dev/sandbox, torn down) must be forecast as near-zero, not
    inflated to look like the partial-tail-day artifact."""
    series = [0.0, 0.02, 0.0, 0.05, 0.0, 0.01, 0.0, 0.0]  # 8 genuinely idle days

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


def test_clean_trailing_partial_day_needs_enough_history():
    """Fewer than SEASON_LEN+1 points: no trailing baseline can be judged, so
    the series passes through unchanged rather than guessing."""
    series = [650.0] * 6 + [5.0]

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


# ── The regression: a flat trailing-median mis-fires on real low/declining days ─
#
# The first fix for the every-7th-day-zero bug replaced series[-1] with the
# flat median of the trailing SEASON_LEN days whenever it looked too low next
# to that median. That conflates different weekdays: on a weekday-high /
# weekend-low cadence the trailing 7 days are mostly highs, so a genuine low
# day reads as "anomalous" and gets inflated to the high value. It also feeds
# a fabricated point into Holt-Winters for a genuinely declining account,
# flattening the real decline. The tests below cover both, plus the same
# genuine-artifact case the original fix was written for.

def test_clean_trailing_partial_day_leaves_a_genuine_weekend_low_alone():
    """Weekday-$1000 / weekend-$100 cadence: the trailing day is a real
    Sunday, identical to every prior Sunday. The old flat-trailing-median
    guard corrupted exactly this shape (5 highs + 2 lows in the trailing
    window medians to $1000, a 10x inflation of a real low day). Comparing
    only to the trailing day's own weekday history, it matches perfectly, so
    it must be left alone."""
    series = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 100.0, 100.0] * 4  # 4 weeks

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


def test_forecast_does_not_inflate_a_genuine_weekly_low_day():
    """End to end: forecasting the same weekday/weekend cadence must not
    blow the low days up into copies of the high days."""
    series = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 100.0, 100.0] * 4

    f = Forecaster(account_id="111122223333").fit(series)
    result = f.predict(horizon_days=14)

    # h=7 and h=14 land on the same weekday as the trailing $100 Sunday.
    for h in (7, 14):
        pt = result.point[h - 1]
        assert pt < 300, (
            f"day {h} (a Sunday) forecasts ${pt}, inflated toward the $1000 "
            f"weekday level")


def test_clean_trailing_partial_day_does_not_inflate_a_real_decline():
    """A fast, steadily winding-down account: series[-1] sits far below its
    own same-weekday history, but only because the whole series is declining
    at a consistent rate, not because of a posting artifact. The decline
    check must recognise the trend and leave it alone."""
    series = [1000.0 * (0.75 ** i) for i in range(21)]  # 3 weeks, winding down fast

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


def test_forecast_does_not_overshoot_a_fast_decline():
    """End to end: Holt-Winters must fit the real decline, not a decline
    flattened by a fabricated same-weekday median plugged into series[-1].

    Uses a milder, longer decline (5%/day over 4 weeks) than the isolated
    guard test above (25%/day over 3 weeks): with only three seasonal
    cycles, Holt-Winters' own tuning is unstable on a very steep decay
    regardless of the partial-day guard (verified separately: the guard
    leaves that series untouched too, and the instability persists). That
    instability is a separate, pre-existing property of the fit/tuning
    code, out of scope for this guard. What the guard is responsible for,
    and what this test checks, is narrower: don't feed the fit a fabricated
    point in the first place.
    """
    series = [1000.0 * (0.95 ** i) for i in range(28)]

    f = Forecaster(account_id="111122223333").fit(series)
    result = f.predict(horizon_days=7)

    # Sanity: day 1 stays near the real last observation instead of jumping
    # back up toward a flat trailing median.
    assert result.point[0] <= series[-1] * 1.5, (
        f"forecast day 1 (${result.point[0]}) jumps well above the real "
        f"last observation (${series[-1]:.2f})")

    # The regression's exact signature: corrupting series[-1] with a flat
    # median reinflates that weekday's seasonal slot, so day 7 (the next
    # occurrence of the same weekday) spikes back up instead of continuing
    # the decline, mirroring the original every-7th-day bug, just inverted.
    # A real, ungarbled decline forecasts smoothly downward instead.
    assert all(a >= b for a, b in zip(result.point, result.point[1:])), (
        f"forecast is not monotonically declining, looks like a day-7 "
        f"seasonal-slot spike: {result.point}")


def test_clean_trailing_partial_day_leaves_a_confirmed_teardown_alone():
    """An account genuinely torn down: its trailing weekday has read $0 for
    multiple prior weeks too, not just the trailing day. That is a confident
    signal of a real, permanent drop (matches its own weekday history
    exactly), so it must stay $0 like any other genuinely idle account."""
    series = _steady_series(14) + [0.0] * 7  # 2 steady weeks, then torn down

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series


def test_clean_trailing_partial_day_cleans_a_lone_zero_right_after_steady_history():
    """A single trailing $0 immediately after weeks of steady, non-zero,
    same-weekday history is the ambiguous case: it could be a same-day
    teardown or a partial-posting read that happened to land on exactly
    zero. We resolve the ambiguity the same way as any other anomalously-low
    trailing day: this is the original bug's exact shape (a steady account
    with a corrupted trailing read), just with the corrupted read landing on
    0.0 instead of a small positive number, so it gets cleaned. A
    *sustained* same-weekday drop (see the confirmed-teardown test above) is
    what distinguishes a real drop from this artifact, and that case is left
    alone."""
    series = _steady_series(21)
    series[-1] = 0.0

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned[-1] != 0.0
    assert cleaned[-1] > 300


def test_clean_trailing_partial_day_skips_when_too_few_same_weekday_samples():
    """More than SEASON_LEN points overall (so the old length gate passes)
    but fewer than 2 prior same-weekday observations: not enough history to
    trust a same-weekday comparison, so the trailing day is left alone
    rather than guessed at. No crash either."""
    series = [650.0] * 9 + [5.0]  # 10 points: only 1 prior same-weekday sample

    cleaned = forecasting._clean_trailing_partial_day(series)

    assert cleaned == series
