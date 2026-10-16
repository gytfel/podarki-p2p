import pytest

from conftest import lot, make_config
from giftbot.evaluator import evaluate, is_anomaly, passes
from giftbot.models import make_key, normalize


def test_normalize_matches_names_across_markets():
    assert normalize("Wizard (1.5%)") == normalize("wizard") == "wizard"
    assert normalize("Durov’s  Cap") == normalize("Durov's Cap")
    assert make_key("Toy Bear", "Wizard (1.5%)") == ("toy bear", "wizard")
    assert make_key("Toy Bear", None) is None


def test_cross_market_profit_counts_all_fees():
    cfg = make_config()
    deal = evaluate(lot("tonnel", "1", 10.0), "portals", 14.0, 4, cfg.fees, cfg.thresholds)
    assert deal.cost == pytest.approx(11.0)  # +10% покупателю на Tonnel
    assert deal.resale_price == pytest.approx(13.86)  # на 1% дешевле конкурента
    assert deal.proceeds == pytest.approx(13.167)  # −5% продавцу на Portals
    assert deal.transfer_cost == pytest.approx(0.3)
    assert deal.profit == pytest.approx(1.867)
    assert deal.profit_pct == pytest.approx(1.867 / 11.0)
    assert deal.cross
    assert passes(deal, cfg.thresholds)


def test_same_market_has_no_transfer_cost():
    cfg = make_config()
    deal = evaluate(lot("portals", "1", 8.0), "portals", 12.0, 3, cfg.fees, cfg.thresholds)
    assert deal.transfer_cost == 0
    assert deal.profit == pytest.approx(12.0 * 0.99 * 0.95 - 8.0)
    assert not deal.cross


def test_small_gap_is_eaten_by_fees():
    cfg = make_config()
    deal = evaluate(lot("tonnel", "1", 12.0), "portals", 14.0, 5, cfg.fees, cfg.thresholds)
    assert deal.profit < 0
    assert not passes(deal, cfg.thresholds)


def test_liquidity_check():
    cfg = make_config()
    deal = evaluate(lot("portals", "1", 8.0), "portals", 12.0, 2, cfg.fees, cfg.thresholds)
    assert not passes(deal, cfg.thresholds)
    assert passes(deal, cfg.thresholds, check_liquidity=False)


def test_slack_relaxes_thresholds():
    cfg = make_config()
    deal = evaluate(lot("portals", "1", 10.0), "portals", 11.0, 5, cfg.fees, cfg.thresholds)
    assert deal.profit == pytest.approx(0.3455)
    assert not passes(deal, cfg.thresholds)
    assert passes(deal, cfg.thresholds, slack=0.5)


def test_anomaly():
    th = make_config().thresholds
    assert is_anomaly(3.0, 14.0, th)
    assert not is_anomaly(4.0, 14.0, th)
    assert not is_anomaly(1.0, 0.0, th)
