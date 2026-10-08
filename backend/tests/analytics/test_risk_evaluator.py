from datetime import date
from decimal import Decimal

from app.risk.evaluator import evaluate_risk_rules


def test_risk_evaluator_triggers_downside_and_weight_rules() -> None:
    events = evaluate_risk_rules(
        {"current_drawdown": Decimal("-0.12"), "max_single_weight": Decimal("0.7")},
        [
            {
                "rule_code": "dd",
                "rule_type": "current_drawdown",
                "threshold": Decimal("0.1"),
            },
            {
                "rule_code": "single",
                "rule_type": "single_position_weight",
                "threshold": Decimal("0.6"),
            },
        ],
        valuation_date_value=date(2026, 1, 1),
        fund_id=7,
    )

    assert [event.rule_code for event in events] == ["dd", "single"]
    assert events[0].observed_value == Decimal("-0.12")
