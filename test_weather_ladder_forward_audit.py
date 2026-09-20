import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research.weather_ladder_forward_audit import market_anchor_scores, run


UTC = timezone.utc


class WeatherLadderForwardAuditTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        self.database = Path(handle.name)
        db = sqlite3.connect(self.database)
        db.executescript(
            """
            CREATE TABLE events(target_date TEXT, city TEXT);
            CREATE TABLE weather_ladder_frozen_candidates(
                target_date TEXT, eligibility_status TEXT, rejection_reasons_json TEXT,
                frozen_slot_utc TEXT, frozen_at_utc TEXT, estimated_fee_usdc REAL,
                net_cost_usdc REAL
            );
            CREATE TABLE weather_ladder_frozen_variants(
                variant_id INTEGER PRIMARY KEY, legs_json TEXT, taker_fee_rate REAL
            );
            CREATE TABLE weather_ladder_frozen_portfolio_selections(
                target_date TEXT, portfolio_version TEXT, variant_id INTEGER,
                selection_status TEXT, resolved_at_utc TEXT,
                hypothetical_pnl_usdc REAL, selected_cost_usdc REAL,
                selected_notional_usdc REAL, selected_fee_usdc REAL,
                winning_range TEXT
            );
            """
        )
        db.executemany(
            "INSERT INTO events VALUES(?,?)",
                [("2026-08-07", "Shanghai"), ("2026-08-07", "Beijing")],
        )
        db.executemany(
            "INSERT INTO weather_ladder_frozen_candidates VALUES(?,?,?,?,?,?,?)",
            [
                ("2026-08-07", "ELIGIBLE_SHADOW", "[]", "2026-08-07T03:00:00+00:00", "2026-08-07T03:03:00+00:00", 0.2, 10.2),
                ("2026-08-07", "REJECTED_SHADOW", '["CENTER_LEAD_BELOW_0.03"]', "2026-08-07T03:00:00+00:00", "2026-08-07T03:03:30+00:00", None, None),
            ],
        )
        legs = json.dumps([
            {"outcomeRange": "34 C", "midpoint": 0.20},
            {"outcomeRange": "35 C", "midpoint": 0.50},
            {"outcomeRange": "36 C", "midpoint": 0.20},
        ])
        db.executemany(
            "INSERT INTO weather_ladder_frozen_variants VALUES(?,?,?)",
            [(1, legs, 0.05), (2, legs, 0.05)],
        )
        db.executemany(
            "INSERT INTO weather_ladder_frozen_portfolio_selections VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-07", "ladder_portfolio_v3_1100_5_20_5_lowest_center_lead", 1, "SELECTED_SHADOW", "2026-08-07T12:00:00+00:00", 9.8, 10.2, 10.0, 0.2, "35 C"),
                ("2026-08-07", "ladder_portfolio_v4_1_1100_5_15_5_all_eligible", 2, "SELECTED_SHADOW", "2026-08-07T12:00:00+00:00", 4.8, 10.2, 10.0, 0.2, "35 C"),
                ("2026-08-07", "ladder_portfolio_v4_1_1100_5_15_5_all_eligible", 2, "SELECTED_SHADOW", "2026-08-07T12:00:00+00:00", 4.8, 10.2, 10.0, 0.2, "35 C"),
            ],
        )
        db.commit()
        db.close()

    def tearDown(self):
        self.database.unlink(missing_ok=True)

    def test_forward_date_is_audited_once_and_uses_net_cost(self):
        with patch("research.weather_ladder_forward_audit.FORWARD_START", date(2026, 8, 7)):
            report = run(self.database, datetime(2026, 8, 7, 4, 30, tzinfo=UTC))
        self.assertEqual(report["due_dates"], 1)
        self.assertEqual(report["quality_pass_dates"], 1)
        self.assertEqual(report["date_audit"][0]["expected_city_events"], 2)
        self.assertEqual(report["portfolios"]["V4.1"]["recorded_dates"], 1)
        self.assertEqual(report["portfolios"]["V4.1"]["recorded_selections"], 2)
        self.assertEqual(report["portfolios"]["V4.1"]["completed_dates"], 1)
        self.assertAlmostEqual(report["portfolios"]["V4.1"]["total_net_cost_usdc"], 20.4)
        self.assertEqual(report["portfolios"]["V4.1"]["outcome_categories"], {"center": 2})

    def test_forward_date_is_not_due_before_capture_deadline(self):
        with patch("research.weather_ladder_forward_audit.FORWARD_START", date(2026, 8, 7)):
            report = run(self.database, datetime(2026, 8, 7, 3, 5, tzinfo=UTC))
        self.assertEqual(report["due_dates"], 0)
        self.assertEqual(report["evidence_status"], "NO_FORWARD_DATES_YET")

    def test_market_anchor_probability_score_is_finite(self):
        score = market_anchor_scores(json.dumps([
            {"outcomeRange": "34 C", "midpoint": 0.20},
            {"outcomeRange": "35 C", "midpoint": 0.50},
            {"outcomeRange": "36 C", "midpoint": 0.20},
        ]), "35 C")
        self.assertIsNotNone(score)
        self.assertGreater(score["log_score"], 0)


if __name__ == "__main__":
    unittest.main()
