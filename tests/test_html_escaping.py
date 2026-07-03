"""Provider-derived strings are escaped before entering generated HTML.

Resource names, tags, and recommendation descriptions come from cloud metadata a
tenant does not fully control. A resource named with an <img onerror> payload
would otherwise be stored HTML/JS in the operator's dashboard or scheduled email.
Regression guard for the P1 finding in the 2026-07 feature security audit.
"""
from finops.reporting.dashboard import _build_html
from finops.notifications.email_digest import _build_html as _build_email

_PAYLOAD = "<img src=x onerror=alert(1)>"
_ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"


def test_dashboard_escapes_resource_names():
    html = _build_html(
        account_id=_PAYLOAD,
        this_month=1000.0, last_month=900.0, projected=1100.0,
        top_services=[{"service": _PAYLOAD, "this_month": 500.0, "last_month": 400.0}],
        opportunities=[{"description": _PAYLOAD, "category": _PAYLOAD, "estimated_monthly_savings_usd": 50.0}],
        savings_summary={"verified_monthly_usd": 0, "acted_on_monthly_usd": 0},
        savings_ledger=[{"description": _PAYLOAD, "source": _PAYLOAD, "status": "verified", "estimated_monthly_savings_usd": 10.0}],
        budgets=[{"name": _PAYLOAD, "pct_used": 50.0, "status": "ok", "limit_usd": 100.0, "spent_usd": 50.0}],
        generated_at="2026-07-03",
    )
    assert "<img src=x onerror" not in html
    assert _ESCAPED in html


def test_email_digest_escapes_anomaly_and_rec_fields():
    html = _build_email(
        period_label="last week",
        total_spend=1000.0, prev_total=900.0,
        top_providers=[{"provider": _PAYLOAD, "amount": 500.0, "pct": 50.0}],
        anomalies=[{"severity": "high", "provider": _PAYLOAD, "service": _PAYLOAD,
                    "direction": "spike", "pct_change": 30.0, "current_amount": 500.0}],
        recommendations=[{"title": _PAYLOAD, "description": _PAYLOAD, "monthly_savings": 100.0}],
    )
    assert "<img src=x onerror" not in html
    assert _ESCAPED in html
