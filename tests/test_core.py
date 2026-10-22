"""Regression tests for the dashboard bugs fixed in this branch."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import elo_engine
import fetch_mlb
import generate_html
import main
import value_calc


def _game(day, home, away, home_won):
    return {
        "date": day,
        "home": home,
        "away": away,
        "home_won": home_won,
        "home_score": 1 if home_won else 0,
        "away_score": 0 if home_won else 1,
    }


class EloRegressionTests(unittest.TestCase):
    def test_new_season_regresses_across_separate_calls(self):
        state = elo_engine._empty_state()
        state = elo_engine.process_games(
            [_game("2025-09-28", "BOS", "NYY", True)], state
        )
        end_2025 = dict(state["ratings"])

        state = elo_engine.process_games(
            [_game("2026-03-28", "BOS", "NYY", True)], state
        )

        regressed = elo_engine.regress_ratings(end_2025)
        expect_home, expect_away = elo_engine.update_elo(
            regressed["BOS"], regressed["NYY"]
        )
        self.assertEqual(state["ratings"]["BOS"], expect_home)
        self.assertEqual(state["ratings"]["NYY"], expect_away)
        self.assertIn(2025, state["seasons_processed"])
        self.assertIn(2026, state["seasons_processed"])
        # The 2026 game must not have been applied on the unregressed rating.
        raw_home, _ = elo_engine.update_elo(end_2025["BOS"], end_2025["NYY"])
        self.assertNotEqual(state["ratings"]["BOS"], raw_home)

    def test_second_game_of_a_season_does_not_regress_again(self):
        state = elo_engine._empty_state()
        state = elo_engine.process_games(
            [_game("2025-09-28", "BOS", "NYY", True)], state
        )
        state = elo_engine.process_games(
            [_game("2026-03-28", "BOS", "NYY", True)], state
        )
        before = dict(state["ratings"])
        state = elo_engine.process_games(
            [_game("2026-03-29", "BOS", "NYY", False)], state
        )
        expect_away, expect_home = elo_engine.update_elo(before["NYY"], before["BOS"])
        self.assertEqual(state["ratings"]["BOS"], expect_home)
        self.assertEqual(state["ratings"]["NYY"], expect_away)

    def test_update_fetches_the_gap_through_yesterday(self):
        state = elo_engine._empty_state()
        state["last_game_date"] = "2025-10-01"
        state["ratings"]["BOS"] = 1600.0
        seen = []

        def fake_range(start, end):
            seen.append((start, end))
            if start.startswith("2026"):
                return [_game("2026-04-02", "BOS", "NYY", True)]
            return []

        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 3)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(elo_engine, "fetch_range_results", fake_range), \
             patch.object(elo_engine, "save_ratings", lambda s: None), \
             patch.object(elo_engine, "ELO_LOG_FILE", Path(tmp) / "log.jsonl"), \
             patch.object(elo_engine, "date", FixedDate):
            updated = elo_engine.update_yesterday(state)

        self.assertEqual(seen, [
            ("2025-10-02", "2025-12-31"),
            ("2026-01-01", "2026-04-02"),
        ])
        self.assertEqual(updated["last_game_date"], "2026-04-02")
        # 1600 regressed by 1/3, then one win, stays well below the raw 1600 path.
        self.assertLess(updated["ratings"]["BOS"], 1590.0)


class ScheduleDocumentTests(unittest.TestCase):
    def test_empty_document_has_games_list(self):
        doc = fetch_mlb.empty_document()
        self.assertEqual(doc["games"], [])
        self.assertEqual(doc["game_count"], 0)
        self.assertIn("date", doc)

    def test_loader_repairs_bare_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "todays_games.json"
            path.write_text("[]")
            with patch.object(fetch_mlb, "OUT_FILE", path):
                doc = main._load_games_doc()
        self.assertEqual(doc["games"], [])
        self.assertIsInstance(doc, dict)


class SignalTests(unittest.TestCase):
    def test_pitcher_edge_sets_bet_and_kelly(self):
        game = {
            "model_prob_home": 0.58,
            "model_prob_away": 0.42,
            "no_vig_prob_home": 0.50,
            "no_vig_prob_away": 0.50,
            "home_fip": 3.10,
            "away_fip": 4.40,
            "home_pitcher": {"record": "10-2"},
            "away_pitcher": {"record": "4-8"},
            "pinnacle_home_odds": -110,
            "pinnacle_away_odds": -110,
        }
        value_calc.compute_signals(game)
        self.assertEqual(game["signal"], "bet")
        self.assertEqual(game["bet_side"], "home")
        self.assertGreater(game["kelly_stake"], 0)

    def test_split_pitcher_metrics_do_not_bet(self):
        game = {
            "model_prob_home": 0.45,
            "model_prob_away": 0.55,
            "no_vig_prob_home": 0.50,
            "no_vig_prob_away": 0.50,
            "home_fip": 3.10,
            "away_fip": 4.40,
            "home_pitcher": {"record": "2-8"},
            "away_pitcher": {"record": "10-2"},
            "pinnacle_home_odds": -110,
            "pinnacle_away_odds": -110,
        }
        value_calc.compute_signals(game)
        self.assertEqual(game["signal"], "none")
        self.assertIsNone(game["kelly_stake"])

    def test_notification_uses_bet_side_edge(self):
        doc = {
            "date": "2026-10-19",
            "games": [{
                "signal": "bet",
                "bet_side": "away",
                "away_abbr": "CLE",
                "home_abbr": "TBR",
                "edge_home": -0.049,
                "edge_away": 0.049,
                "kelly_stake": 0.012,
                "bullpen_flag_away": True,
                "bullpen_flag_home": False,
            }],
        }
        msg = main._result_message(doc, {"bet": 1, "value": 0, "flag": 0})
        self.assertIn("CLE@TBR away +4.9% Kelly=1.2%", msg)
        self.assertIn("1 bullpen", msg)
        self.assertNotIn("-4.9%", msg)


class HtmlTests(unittest.TestCase):
    def _game(self):
        return {
            "signal": "bet",
            "bet_side": "away",
            "home_abbr": "TBR",
            "away_abbr": "CLE",
            "home_name": "Tampa Bay Rays",
            "away_name": "Cleveland Guardians",
            "home_pitcher": {"name": "Freddy Peralta", "record": "10-11"},
            "away_pitcher": {"name": "Shawn Armstrong", "record": "2-1"},
            "edge_home": -0.049,
            "edge_away": 0.049,
            "kelly_stake": 0.012,
            "game_date": "2026-10-26T00:08:00Z",
            "model_prob_away": 0.468,
            "model_prob_home": 0.532,
            "no_vig_prob_away": 0.419,
            "no_vig_prob_home": 0.581,
            "pinnacle_away_odds": 134,
            "pinnacle_home_odds": -145,
            "away_fip": 4.48,
            "home_fip": 4.33,
            "bullpen_flag_away": True,
            "bullpen_flag_home": False,
            "park_flag": False,
        }

    def test_negative_edge_uses_red_class_and_kelly_shows(self):
        html = generate_html.game_row(self._game())
        self.assertIn('class="en en2"', html)
        self.assertIn("1.2%", html)
        self.assertIn("fogadj", html)

    def test_budapest_dst(self):
        # 2026-10-20 is still CEST (UTC+2). DST ends on 2026-10-25.
        self.assertEqual(generate_html.to_hu_time("2026-10-20T00:08:00Z"), "02:08")
        self.assertEqual(generate_html.to_hu_time("2026-10-26T00:08:00Z"), "01:08")

    def test_footer_matches_bet_rule(self):
        doc = {
            "games": [self._game()],
            "date": "2026-10-26",
            "generated_at": "2026-10-26T12:00:00",
            "elo_last_date": "2026-09-28",
            "elo_games_processed": 100,
        }
        html = generate_html.render(doc)
        self.assertIn("pozitív edge", html)
        self.assertIn("Elo alapszakasz", html)
        self.assertNotIn("4–10%", html)
        self.assertNotIn("kizárás", html)

    def test_empty_slate_says_there_is_no_game(self):
        doc = {
            "games": [],
            "date": "2026-10-22",
            "generated_at": "2026-10-22T18:47:00",
            "elo_last_date": "2026-09-27",
            "elo_games_processed": 12162,
        }
        html = generate_html.render(doc)
        self.assertIn("Nincs mai mérkőzés.", html)
        self.assertIn(">0<", html)


if __name__ == "__main__":
    unittest.main()
