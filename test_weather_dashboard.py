import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from weather_dashboard import server


class WeatherDashboardLadderV4Tests(unittest.TestCase):
    def setUp(self):
        db_file = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        db_file.close()
        history_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        history_file.close()
        self.database = Path(db_file.name)
        self.history = Path(history_file.name)
        db = sqlite3.connect(self.database)
        db.executescript(
            """
            CREATE TABLE weather_ladder_frozen_candidates(
                candidate_id INTEGER PRIMARY KEY,target_date TEXT,eligibility_status TEXT,
                lower_bucket_c INTEGER,center_bucket_c INTEGER,upper_bucket_c INTEGER,
                lower_spread REAL,center_spread REAL,upper_spread REAL
            );
            CREATE TABLE weather_ladder_frozen_variants(
                variant_id INTEGER PRIMARY KEY,legs_json TEXT,
                lower_weight REAL,center_weight REAL,upper_weight REAL
            );
            CREATE TABLE weather_ladder_frozen_portfolio_selections(
                target_date TEXT,frozen_slot_utc TEXT,selected_at_utc TEXT,
                eligible_candidates INTEGER,selection_status TEXT,rejection_reasons_json TEXT,
                event_id TEXT,city TEXT,selected_cost_usdc REAL,selected_notional_usdc REAL,
                selected_fee_usdc REAL,resolved_at_utc TEXT,winning_range TEXT,payout_usdc REAL,
                hypothetical_pnl_usdc REAL,variant_id INTEGER,candidate_id INTEGER,
                portfolio_version TEXT
            );
            """
        )
        db.execute(
            "INSERT INTO weather_ladder_frozen_candidates VALUES(1,'2026-08-07','ELIGIBLE_SHADOW',34,35,36,0.02,0.03,0.04)"
        )
        legs = json.dumps([
            {"outcomeRange": "34 C", "weight": 5, "vwap": 0.2},
            {"outcomeRange": "35 C", "weight": 15, "vwap": 0.4},
            {"outcomeRange": "36 C", "weight": 5, "vwap": 0.2},
        ])
        db.execute("INSERT INTO weather_ladder_frozen_variants VALUES(1,?,5,15,5)", (legs,))
        db.execute(
            """
            INSERT INTO weather_ladder_frozen_portfolio_selections VALUES(
                '2026-08-07','2026-08-07T03:00:00+00:00','2026-08-07T03:03:00+00:00',
                1,'SELECTED_SHADOW','[]','event-1','Shanghai',8.2,8.0,0.2,
                '2026-08-07T12:00:00+00:00','35 C',15,6.8,1,1,
                'ladder_portfolio_v4_1_1100_5_15_5_all_eligible'
            )
            """
        )
        db.commit()
        db.close()
        self.history.write_text(json.dumps({
            "executable_min_5_share_structures": {
                "5/15/5": {"independent_dates": 15, "roi": 0.162, "total_pnl": 67.747}
            },
        }), encoding="utf-8")

    def tearDown(self):
        self.database.unlink(missing_ok=True)
        self.history.unlink(missing_ok=True)

    def test_ladder_v4_endpoint_data_separates_forward_and_history(self):
        with patch.object(server, "DB_PATH", self.database), patch.object(server, "LADDER_HISTORY_PATH", self.history):
            payload = server.WeatherDashboardData().ladder_v4()
        self.assertTrue(payload["shadow_only"])
        self.assertEqual(payload["summary"]["completed_dates"], 1)
        self.assertAlmostEqual(payload["summary"]["net_roi"], 6.8 / 8.2)
        self.assertEqual(payload["summary"]["outcome_counts"]["center"], 1)
        self.assertEqual(payload["historical"]["metrics"]["independent_dates"], 15)
        self.assertEqual(payload["records"][0]["city"], "Shanghai")

    def test_bootstrap_lower_bound_is_empty_without_forward_dates(self):
        self.assertIsNone(server.WeatherDashboardData._bootstrap_p05([]))


if __name__ == "__main__":
    unittest.main()
