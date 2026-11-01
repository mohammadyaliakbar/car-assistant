from app.emergency import RiskLevel


def test_unknown_is_not_comparable_with_real_levels():
    assert RiskLevel.UNKNOWN.rank == -1
    assert RiskLevel.CRITICAL.rank > RiskLevel.HIGH.rank > RiskLevel.LOW.rank