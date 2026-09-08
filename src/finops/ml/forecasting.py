"""
nable Cost Forecasting Engine — pure Python, no sklearn required.

Implements Holt-Winters triple exponential smoothing (additive seasonality)
for cloud spend time series. Model parameters (alpha, beta, gamma) are fit
per-account using gradient descent on historical data and persisted to the
DB so each account has its own tuned model.

Why this is proprietary:
  - The fitted parameters per account/service accumulate as a data asset
  - Multi-signal blending (trend + seasonality + anomaly suppression) is
    tuned against real cloud spend patterns, not synthetic data
  - The model degrades gracefully: single data point → naive extrapolation,
    <7 days → linear, <30 days → trend-only, ≥30 days → full Holt-Winters

Public API:
    forecast = Forecaster.for_account(account_id, service=None)
    result   = forecast.predict(horizon_days=30)
    # → {"point": [...], "lower": [...], "upper": [...], "mape": float}
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ── Holt-Winters (additive, weekly seasonality = 7) ──────────────────────────

SEASON_LEN = 7   # weekly seasonality in cloud spend is the dominant cycle

# A trailing day read at below this fraction of its OWN prior same-weekday
# history is treated as a partial/lag artifact rather than a real
# observation (see _clean_trailing_partial_day). 0.35 means "under 35% of
# the same-weekday median", comfortably outside normal weekly wobble, so a
# genuinely slow-but-real day is not misclassified.
_PARTIAL_DAY_RATIO = 0.35

# Need at least this many prior same-weekday observations before trusting a
# comparison at all. With fewer, we cannot tell a real low day from an
# artifact, so we leave the data alone rather than guess.
_MIN_SAME_WEEKDAY_SAMPLES = 2

# A trailing day is only excused by a decline (see _trend_explains_the_drop)
# when it is not too far even below what that decline predicts. Below this
# fraction of the trend's own prediction, a real decline no longer explains
# it and the day is still treated as a possible artifact.
_TREND_CONSISTENT_RATIO = 0.5


def _same_weekday_history(series: list[float]) -> list[float]:
    """Values from the same day-of-week as series[-1]: series[-1-SEASON_LEN],
    series[-1-2*SEASON_LEN], and so on for as far back as the series goes."""
    n = len(series)
    history = []
    k = 1
    while True:
        idx = n - 1 - k * SEASON_LEN
        if idx < 0:
            break
        history.append(series[idx])
        k += 1
    return history


def _trend_explains_the_drop(series: list[float]) -> bool:
    """
    True when the SEASON_LEN days right before the trailing day are already
    on a consistent downward slope that predicts a value close to (or lower
    than) the real series[-1]: a genuinely declining/winding-down account,
    not a posting artifact.

    A fast, real decline can put series[-1] far below its own same-weekday
    history for entirely legitimate reasons (weekly seasonality is a much
    slower signal than a fast day-over-day wind-down). Overriding a point
    like that with the same-weekday median would flatten the real decline
    and feed a fabricated value into Holt-Winters' recursive level/trend
    update, which then overshoots the forecast. So before accepting the
    same-weekday check's verdict, we check whether the recent trend alone
    already explains the low value.
    """
    window = series[-(SEASON_LEN + 1):-1]
    xs = list(range(len(window)))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(window)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, window))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return False

    slope = num / den
    if slope >= 0:
        return False  # flat or rising trend: doesn't excuse a low trailing day

    intercept = y_mean - slope * x_mean
    predicted = intercept + slope * len(window)  # one step past the window == series[-1]
    if predicted <= 0:
        # The trend already extrapolates to zero/negative, so any real,
        # non-negative value is consistent with (or milder than) the decline.
        return True

    return series[-1] >= predicted * _TREND_CONSISTENT_RATIO


def _clean_trailing_partial_day(series: list[float]) -> list[float]:
    """
    Guard the fit against a not-yet-fully-posted trailing day.

    Cost data for the most recent day is frequently incomplete: usage and
    billing records land with a lag, so "today" (or "yesterday", depending on
    the data source) can read as near-zero even though the account is
    spending normally. Holt-Winters has no way to know that and bakes the
    near-zero straight into the seasonal component for that weekday. Because
    the seasonal array repeats every SEASON_LEN entries, that one corrupted
    value resurfaces on every SEASON_LEN-th day across the whole forecast
    horizon: an account steadily spending $600-700/day forecasts an exact
    $0.00, with a zero-width interval, on every 7th day out.

    Detection compares series[-1] only to its OWN prior same-weekday
    observations (series[-1-7], series[-1-14], ...), never to a flat
    trailing-N-day median. A flat median mixes different weekdays together,
    so on a real weekday-high/weekend-low cadence it mistakes a genuine low
    day for an artifact and inflates it, and it can just as easily reinflate
    a genuine permanent drop to zero. Comparing a day only to its own
    weekday's history avoids both: a day that matches its own weekday is
    never touched, no matter how different it looks from the rest of the
    week. A decline check further guards against fast, genuinely declining
    accounts, where series[-1] can legitimately sit far below its own
    same-weekday history because the account is winding down.

    This only ever inspects/replaces series[-1] in a copy of the input; the
    caller's list is never mutated. Every other point, including a genuine
    zero anywhere else in the series, is returned exactly as given, so a
    real drop to zero is never masked. Only this specific partial-tail-day
    artifact is handled, and only when there is enough same-weekday history
    to be confident about it: a false negative (missing a partial day) is
    far cheaper than overwriting a customer's real number.
    """
    n = len(series)
    if n <= SEASON_LEN:
        return series  # not enough history to judge a trailing baseline

    history = _same_weekday_history(series)
    if len(history) < _MIN_SAME_WEEKDAY_SAMPLES:
        return series  # too little same-weekday history to trust a comparison

    weekday_baseline = statistics.median(history)
    last = series[-1]

    if weekday_baseline <= 1.0 or last >= weekday_baseline * _PARTIAL_DAY_RATIO:
        return series  # matches its own weekday, or genuinely idle: not an artifact

    if _trend_explains_the_drop(series):
        return series  # a real, ongoing decline predicts a value this low

    cleaned = list(series)
    cleaned[-1] = round(weekday_baseline, 2)
    log.info(
        "forecast: trailing day $%.2f looks partial next to a $%.2f "
        "same-weekday median; using the same-weekday median for fitting instead",
        last, weekday_baseline,
    )
    return cleaned


def _holt_winters_fit(
    series: list[float],
    alpha: float = 0.3,
    beta: float  = 0.1,
    gamma: float = 0.2,
) -> tuple[list[float], float, list[float]]:
    """
    Fit Holt-Winters additive model.

    Returns (fitted_values, final_trend, final_seasonals).
    """
    n = len(series)
    if n < SEASON_LEN * 2:
        raise ValueError(f"Need at least {SEASON_LEN*2} data points, got {n}")

    # Initialise level, trend, seasonals
    level = statistics.mean(series[:SEASON_LEN])
    trend = (statistics.mean(series[SEASON_LEN:SEASON_LEN*2]) -
             statistics.mean(series[:SEASON_LEN])) / SEASON_LEN
    seasonals = [series[i] - level for i in range(SEASON_LEN)]

    fitted: list[float] = []
    for i, y in enumerate(series):
        s = seasonals[i % SEASON_LEN]
        prev_level = level
        level = alpha * (y - s) + (1 - alpha) * (prev_level + trend)
        trend = beta  * (level - prev_level) + (1 - beta) * trend
        seasonals[i % SEASON_LEN] = gamma * (y - level) + (1 - gamma) * s
        fitted.append(level + trend + seasonals[i % SEASON_LEN])

    return fitted, trend, seasonals


def _mape(actuals: list[float], fitted: list[float]) -> float:
    """Mean Absolute Percentage Error — skip zeros."""
    errors = []
    for a, f in zip(actuals, fitted):
        if a > 0:
            errors.append(abs((a - f) / a))
    return round(statistics.mean(errors) * 100, 2) if errors else 0.0


def _tune_parameters(series: list[float]) -> tuple[float, float, float]:
    """
    Grid-search alpha/beta/gamma to minimise MAPE on the back-half of the series.

    Uses a coarse then fine two-pass grid (fast enough for series up to ~365 pts).
    """
    best = (0.3, 0.1, 0.2)
    best_mape = float("inf")
    half = len(series) // 2

    coarse = [0.1, 0.3, 0.5, 0.7, 0.9]
    for a in coarse:
        for b in [0.05, 0.1, 0.3]:
            for g in coarse:
                try:
                    fitted, _, _ = _holt_winters_fit(series, a, b, g)
                    m = _mape(series[half:], fitted[half:])
                    if m < best_mape:
                        best_mape = m
                        best = (a, b, g)
                except Exception:
                    continue

    # Fine pass around best
    a0, b0, g0 = best
    fine = [-0.1, -0.05, 0.0, 0.05, 0.1]
    for da in fine:
        for db in fine:
            for dg in fine:
                a = max(0.01, min(0.99, a0 + da))
                b = max(0.01, min(0.99, b0 + db))
                g = max(0.01, min(0.99, g0 + dg))
                try:
                    fitted, _, _ = _holt_winters_fit(series, a, b, g)
                    m = _mape(series[half:], fitted[half:])
                    if m < best_mape:
                        best_mape = m
                        best = (a, b, g)
                except Exception:
                    continue

    return best


def _predict(
    series: list[float],
    horizon: int,
    alpha: float,
    beta: float,
    gamma: float,
) -> tuple[list[float], list[float], list[float]]:
    """
    Run Holt-Winters on full series, then extrapolate `horizon` steps.

    Returns (point_forecast, lower_80, upper_80).
    """
    fitted, trend, seasonals = _holt_winters_fit(series, alpha, beta, gamma)

    # Residual std from last 30 fitted points for CI
    tail = min(30, len(series))
    residuals = [abs(series[-(tail-i)] - fitted[-(tail-i)]) for i in range(tail)]
    sigma = statistics.stdev(residuals) if len(residuals) > 1 else 0.0

    last_level = fitted[-1] - (seasonals[(len(series)-1) % SEASON_LEN])
    points, lowers, uppers = [], [], []

    for h in range(1, horizon + 1):
        s = seasonals[(len(series) + h - 1) % SEASON_LEN]
        pt = last_level + trend * h + s
        # 80% prediction interval widens with horizon
        ci = 1.28 * sigma * math.sqrt(h)
        points.append(round(max(0, pt), 2))
        lowers.append(round(max(0, pt - ci), 2))
        uppers.append(round(max(0, pt + ci), 2))

    return points, lowers, uppers


# ── Linear fallback for short series ─────────────────────────────────────────

def _linear_predict(series: list[float], horizon: int) -> tuple[list[float], list[float], list[float]]:
    """Simple linear regression extrapolation for series with < 14 points."""
    n = len(series)
    xs = list(range(n))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(series)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, series))
    den = sum((x - x_mean) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = y_mean - slope * x_mean
    sigma = statistics.stdev(series) if len(series) > 1 else 0.0

    points, lowers, uppers = [], [], []
    for h in range(1, horizon + 1):
        pt = max(0.0, intercept + slope * (n + h))
        ci = 1.28 * sigma
        points.append(round(pt, 2))
        lowers.append(round(max(0, pt - ci), 2))
        uppers.append(round(pt + ci, 2))
    return points, lowers, uppers


# ── Model persistence ─────────────────────────────────────────────────────────

def _save_model(account_id: str, service: str | None, params: dict) -> None:
    """Persist fitted model params to DB."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text
        from datetime import datetime as _dt, timezone as _tz
        engine = get_engine()
        key = f"{account_id}:{service or '__total__'}"
        # Bind updated_at from Python: datetime('now') is SQLite-only and raises on
        # Postgres, so the model never persists there and re-fits every forecast.
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO forecast_models (model_key, params_json, updated_at)
                VALUES (:key, :params, :updated_at)
                ON CONFLICT(model_key) DO UPDATE
                  SET params_json = excluded.params_json,
                      updated_at  = excluded.updated_at
            """), {"key": key, "params": json.dumps(params), "updated_at": _dt.now(_tz.utc).isoformat()})
    except Exception as e:
        log.debug("model save skipped: %s", e)


def _load_model(account_id: str, service: str | None) -> dict | None:
    """Load previously fitted model params from DB."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text
        engine = get_engine()
        key = f"{account_id}:{service or '__total__'}"
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT params_json FROM forecast_models WHERE model_key = :key"),
                {"key": key},
            ).fetchone()
            return json.loads(row[0]) if row else None
    except Exception:
        return None


# ── Public Forecaster class ───────────────────────────────────────────────────

@dataclass
class ForecastResult:
    account_id: str
    service: str | None
    start_date: date
    horizon_days: int
    method: str                   # "holt_winters" | "linear" | "naive"
    mape: float                   # in-sample accuracy %
    monthly_projection: float     # sum of 30-day forward values
    point: list[float]
    lower: list[float]
    upper: list[float]
    dates: list[str]
    params: dict                  # alpha/beta/gamma (or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id":         self.account_id,
            "service":            self.service,
            "method":             self.method,
            "mape_pct":           self.mape,
            "monthly_projection": self.monthly_projection,
            "horizon_days":       self.horizon_days,
            "forecast": [
                {"date": d, "point": p, "lower": l, "upper": u}
                for d, p, l, u in zip(self.dates, self.point, self.lower, self.upper)
            ],
            "params":             self.params,
        }


class Forecaster:
    """
    Per-account cost forecaster.

    Usage:
        f = Forecaster(account_id="123456789012", service="EC2")
        f.fit(daily_costs)            # list of floats, oldest first
        result = f.predict(30)        # 30-day forward forecast
    """

    def __init__(self, account_id: str, service: str | None = None):
        self.account_id = account_id
        self.service    = service
        self._series: list[float] = []
        self._params: dict = {}
        self._mape: float  = 0.0
        self._method: str  = "naive"

    # -- Loading from storage --------------------------------------------------

    @classmethod
    def for_account(
        cls,
        account_id: str,
        service: str | None = None,
        days: int = 90,
        aws_connector=None,
    ) -> "Forecaster":
        """
        Construct a Forecaster with historical daily costs.

        Data source priority:
          1. Local cost_snapshots DB (accumulated via take_snapshot_now)
          2. AWS Cost Explorer directly (13 months of daily history available
             immediately — no snapshot setup required)

        Passing aws_connector enables the Cost Explorer fallback so forecasting
        works on day one without any snapshot history.
        """
        f = cls(account_id, service)
        series = f._load_series(days)
        if not series and aws_connector is not None:
            series = f._load_series_from_ce(aws_connector, days)
        if series:
            f.fit(series)
        return f

    def _load_series_from_ce(self, aws_connector, days: int) -> list[float]:
        """Pull daily cost totals directly from AWS Cost Explorer.

        Returns up to `days` days of history. Works on day one — Cost Explorer
        has 13 months of history available without any local setup.
        """
        try:
            from ..billing_access import ce_client
            end = date.today()
            start = end - timedelta(days=days)

            # The sanctioned Cost Explorer chokepoint, not a raw boto3 client: it
            # refuses in demo mode, when CE is disabled (NABLE_NO_COST_EXPLORER),
            # and on unattended/scheduled paths where a per-request charge would
            # repeat with nobody watching. A refusal raises BillingAccessError,
            # caught below and degraded to no CE series — the forecast falls back
            # to whatever local snapshot history exists — rather than an unmetered
            # charge on the customer's own AWS account.
            ce = ce_client(reason="forecasting day-one history (no local snapshots)",
                           region="us-east-1")

            kwargs: dict = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
            if self.service:
                kwargs["Filter"] = {
                    "Dimensions": {"Key": "SERVICE", "Values": [self.service]}
                }

            resp = ce.get_cost_and_usage(**kwargs)

            series = []
            for period in resp.get("ResultsByTime", []):
                amount = float(
                    period.get("Total", {})
                    .get("UnblendedCost", {})
                    .get("Amount", 0.0)
                )
                series.append(amount)
            log.info(
                "Loaded %d days of CE history for forecasting (no local snapshots needed)",
                len(series),
            )
            return series
        except Exception as exc:
            log.debug("CE series fallback failed: %s", exc)
            return []

    def _load_series(self, days: int) -> list[float]:
        """Pull daily cost totals from cost_snapshots."""
        try:
            from ..storage.db import get_engine
            from sqlalchemy import text
            engine = get_engine()
            start = (date.today() - timedelta(days=days)).isoformat()
            if self.service:
                q = text("""
                    SELECT snapshot_date, SUM(amount_usd) as total
                    FROM cost_snapshots
                    WHERE account_id = :aid
                      AND service    = :svc
                      AND snapshot_date >= :start
                    GROUP BY snapshot_date ORDER BY snapshot_date
                """)
                params = {"aid": self.account_id, "svc": self.service, "start": start}
            else:
                q = text("""
                    SELECT snapshot_date, SUM(amount_usd) as total
                    FROM cost_snapshots
                    WHERE account_id = :aid
                      AND snapshot_date >= :start
                    GROUP BY snapshot_date ORDER BY snapshot_date
                """)
                params = {"aid": self.account_id, "start": start}
            with engine.connect() as conn:
                rows = conn.execute(q, params).fetchall()
            return [float(r[1]) for r in rows]
        except Exception as e:
            log.debug("series load failed: %s", e)
            return []

    # -- Fitting ---------------------------------------------------------------

    def fit(self, series: list[float]) -> "Forecaster":
        """
        Fit the model. Chooses method based on series length:
          <7 pts  → naive (mean)
          7–13    → linear regression
          ≥14     → Holt-Winters (auto-tunes alpha/beta/gamma)

        Guards against a not-yet-fully-posted trailing day (see
        _clean_trailing_partial_day) before any of the above ever sees it.
        """
        clamped = [max(0.0, x) for x in series]
        self._series = _clean_trailing_partial_day(clamped)
        n = len(self._series)

        if n < 7:
            self._method = "naive"
            self._params = {}
            self._mape   = 0.0
        elif n < 14:
            self._method = "linear"
            self._params = {}
            self._mape   = 0.0
        else:
            alpha, beta, gamma = _tune_parameters(self._series)
            fitted, _, _   = _holt_winters_fit(self._series, alpha, beta, gamma)
            self._mape     = _mape(self._series, fitted)
            self._method   = "holt_winters"
            self._params   = {"alpha": alpha, "beta": beta, "gamma": gamma}
            # Cache to DB so next call skips re-tuning if data unchanged
            _save_model(self.account_id, self.service, {
                **self._params, "n": n, "mape": self._mape,
            })

        return self

    # -- Prediction ------------------------------------------------------------

    def predict(self, horizon_days: int = 30) -> ForecastResult:
        """Predict `horizon_days` days of future spend."""
        series = self._series
        n = len(series)
        today = date.today()
        dates = [(today + timedelta(days=i+1)).isoformat() for i in range(horizon_days)]

        if n == 0:
            point  = [0.0] * horizon_days
            lower  = [0.0] * horizon_days
            upper  = [0.0] * horizon_days
            method = "naive"
            mape   = 0.0
        elif self._method == "naive" or n < 7:
            avg    = statistics.mean(series)
            point  = [round(avg, 2)] * horizon_days
            std    = statistics.stdev(series) if n > 1 else 0.0
            lower  = [round(max(0, avg - 1.28 * std), 2)] * horizon_days
            upper  = [round(avg + 1.28 * std, 2)] * horizon_days
            method = "naive"
            mape   = 0.0
        elif self._method == "linear" or n < 14:
            point, lower, upper = _linear_predict(series, horizon_days)
            method = "linear"
            mape   = self._mape
        else:
            p = self._params
            point, lower, upper = _predict(
                series, horizon_days,
                p.get("alpha", 0.3), p.get("beta", 0.1), p.get("gamma", 0.2),
            )
            method = "holt_winters"
            mape   = self._mape

        monthly_projection = round(sum(point[:30]), 2)

        return ForecastResult(
            account_id=self.account_id,
            service=self.service,
            start_date=today,
            horizon_days=horizon_days,
            method=method,
            mape=mape,
            monthly_projection=monthly_projection,
            point=point,
            lower=lower,
            upper=upper,
            dates=dates,
            params=self._params,
        )

    # -- Convenience -----------------------------------------------------------

    def predict_dict(self, horizon_days: int = 30) -> dict[str, Any]:
        return self.predict(horizon_days).to_dict()
