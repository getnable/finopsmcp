# SPDX-License-Identifier: Apache-2.0
"""forecast MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv

# A daily forecast past a year is noise, and every day is one row in the answer.
_MAX_HORIZON_DAYS = 365
_MIN_HISTORY_DAYS = 14


@_srv.mcp.tool()
async def forecast_costs(
    account_id: str | None = None,
    service: str | None = None,
    horizon_days: int = 30,
    history_days: int = 90,
) -> dict:
    """
    Forecast future cloud spend using Holt-Winters time-series modelling.

    Automatically tunes forecast parameters (alpha/beta/gamma) to your account's
    historical spend patterns and returns a daily point forecast with 80%
    prediction intervals.

    Args:
        account_id:   AWS account ID (auto-discovered from STS if not provided)
        service:      specific service to forecast (e.g. "EC2", "RDS"), omit for total
        horizon_days: number of days to forecast, 1 to 365 (default 30)
        history_days: days of history to fit the model, 14 to 365 (default 90)

    Returns forecast including method used, MAPE accuracy %, monthly projection,
    and day-by-day point/lower/upper estimates, all in USD.
    Examples:
        - "Forecast our AWS spend for next month"
        - "Where will EC2 costs be in 60 days?"

    """
    # Bounds first, before any model is fitted. horizon_days=100000 returned one
    # daily point per day, 10.6 MB of JSON into the model's context, and a
    # horizon of 0 or less returned monthly_projection 0.0: a $0 figure made
    # from a bad argument.
    if not 1 <= horizon_days <= _MAX_HORIZON_DAYS:
        return {"error": (f"horizon_days must be between 1 and {_MAX_HORIZON_DAYS} "
                          f"(got {horizon_days}).")}
    if not _MIN_HISTORY_DAYS <= history_days <= _MAX_HORIZON_DAYS:
        return {"error": (f"history_days must be between {_MIN_HISTORY_DAYS} and "
                          f"{_MAX_HORIZON_DAYS} (got {history_days}): a forecast needs "
                          f"at least {_MIN_HISTORY_DAYS} days of daily spend to fit.")}
    if (err := _srv.require_pro("forecasting")):
        return err
    try:
        from ..ml.forecasting import Forecaster
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        aws_configured = aws and await aws.is_configured()
        account_id = await _srv._resolve_account_id(account_id)
        if not account_id:
            return {
                "error": "No account_id provided and none could be auto-discovered.",
                "hint": ("Connect AWS right here in the chat with connect_aws, or pass "
                         "account_id explicitly."),
            }
        # to_thread: for_account is synchronous and slow, a DB read, possibly a
        # Cost Explorer call, then a Holt-Winters parameter grid search. On the
        # event loop it froze every other tool call for the duration.
        f = await _srv.asyncio.to_thread(
            Forecaster.for_account,
            account_id,
            service=service,
            days=history_days,
            aws_connector=aws if aws_configured else None,
        )
        if not f._series:
            if aws_configured:
                # Connected, just nothing to fit yet. "Connect your AWS account"
                # sent a connected user back to a setup they had already done.
                return {
                    "error": (
                        f"No cost history yet for account {account_id}"
                        + (f" ({service})" if service else "")
                        + ": nable has no daily snapshots for it and Cost Explorer "
                        "returned no daily spend to fit a forecast to."),
                    "hint": (
                        "Forecasting needs at least 14 days of daily spend. Run "
                        "take_snapshot_now to start recording history, and use "
                        "explain_recent_cost_drivers or get_cost_summary to see "
                        "current spend in the meantime."),
                }
            return {
                "error": "No historical data found for this account/service.",
                "hint": ("AWS is not connected. Connect it right here in the chat: "
                         "call connect_aws (it detects credentials already on this "
                         "machine)."),
            }
        return await _srv.asyncio.to_thread(f.predict_dict, horizon_days)
    except Exception as e:
        return {"error": str(e)}
