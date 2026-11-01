"""Identity collision and coverage tests for the offline fixture candidate map."""
import copy
import unittest

from titan_fixture_mapping import map_fixture_candidates


def fixture(event="100", home="Arsenal", away="Chelsea", kickoff="2026-10-31T15:00:00Z", **extra):
    row = {"provider": "espn", "match_id": "espn:" + event, "source_event_id": event,
           "league": "england-premier", "home_id": home, "away_id": away,
           "kickoff_at": kickoff, "status": "scheduled", "problems": []}
    return dict(row, **extra)


def titan(event="3004000", home="Arsenal", away="Chelsea", kickoff="2026-10-31T15:00:00Z", **extra):
    row = {"provider_event_id": event, "league_id": "36", "home_id": home, "away_id": away,
           "home": "阿森纳", "away": "切尔西", "kickoff_at": kickoff}
    return dict(row, **extra)


def match(fixtures, candidates):
    return map_fixture_candidates(fixtures, candidates, titan_league_id="36")


class FixtureMappingTests(unittest.TestCase):
    def test_exact_match_remains_unverified(self):
        result = match([fixture()], [titan()])
        row = result["coverage"][0]
        self.assertEqual(row["state"], "mapped_candidate")
        self.assertEqual(row["candidate"]["provider_event_id"], "3004000")
        self.assertFalse(row["mapping_verified"])
        self.assertTrue(row["requires_ah_detail_validation"])
        self.assertFalse(result["formal_observation"])
        self.assertFalse(result["training_eligible"])

    def test_missing_match_is_counted_and_retained(self):
        result = match([fixture(), fixture("101", "Liverpool", "Everton")], [titan()])
        self.assertEqual(len(result["coverage"]), 2)
        self.assertEqual(result["counts"]["expected"], 2)
        self.assertEqual(result["counts"]["mapped"], 1)
        self.assertEqual(result["counts"]["missing"], 1)
        self.assertIn("no_matching_titan_candidate", result["coverage"][1]["reasons"])

    def test_equivalent_explicit_timezones_match(self):
        result = match([fixture()], [titan(kickoff="2026-10-31T23:00:00+08:00")])
        self.assertEqual(result["counts"]["mapped"], 1)

    def test_reschedule_even_one_second_is_not_guessed(self):
        for timestamp in ("2026-10-31T15:00:01Z", "2026-11-01T15:00:00Z"):
            with self.subTest(timestamp=timestamp):
                result = match([fixture()], [titan(kickoff=timestamp)])
                self.assertEqual(result["counts"]["mapped"], 0)
                self.assertEqual(result["coverage"][0]["reasons"], ["same_teams_but_kickoff_mismatch"])

    def test_same_teams_different_dates_map_to_distinct_events(self):
        result = match([fixture(), fixture("101", kickoff="2027-01-01T15:00:00Z")],
                       [titan(), titan("3004001", kickoff="2027-01-01T15:00:00Z")])
        self.assertEqual(result["counts"]["mapped"], 2)
        self.assertEqual([r["candidate"]["provider_event_id"] for r in result["coverage"]],
                         ["3004000", "3004001"])

    def test_reversed_sides_are_not_accepted(self):
        result = match([fixture()], [titan(home="Chelsea", away="Arsenal")])
        self.assertEqual(result["coverage"][0]["reasons"], ["only_reversed_home_away_candidates"])

    def test_duplicate_titan_rows_are_ambiguous_even_when_identical(self):
        result = match([fixture()], [titan(), titan()])
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertEqual(result["counts"]["mapped"], 0)

    def test_two_titan_events_for_same_fixture_are_ambiguous(self):
        result = match([fixture()], [titan(), titan("3004001")])
        self.assertEqual(result["counts"]["ambiguous"], 1)

    def test_titan_id_collision_on_other_date_blocks_both(self):
        result = match([fixture(), fixture("101", kickoff="2027-01-01T15:00:00Z")],
                       [titan(), titan(kickoff="2027-01-01T15:00:00Z")])
        self.assertEqual(result["counts"]["ambiguous"], 2)
        for row in result["coverage"]:
            self.assertIn("duplicate_or_conflicting_titan_event_id", row["reasons"])

    def test_invalid_duplicate_id_also_blocks_valid_candidate(self):
        result = match([fixture()], [titan(), titan(home_id=None)])
        self.assertEqual(result["counts"]["ambiguous"], 1)

    def test_duplicate_expected_id_is_retained_without_double_mapping(self):
        result = match([fixture(), fixture()], [titan()])
        self.assertEqual(result["counts"]["expected"], 2)
        self.assertEqual(result["counts"]["unique_expected_match_ids"], 1)
        self.assertEqual(result["counts"]["ambiguous"], 2)

    def test_different_espn_ids_for_same_identity_are_ambiguous(self):
        result = match([fixture(), fixture("101")], [titan()])
        self.assertEqual(result["counts"]["ambiguous"], 2)

    def test_wrong_league_never_maps(self):
        for expected, candidate in ((fixture(league="england-championship"), titan()),
                                    (fixture(), titan(league_id="37"))):
            with self.subTest(expected=expected, candidate=candidate):
                result = match([expected], [candidate])
                self.assertEqual(result["counts"]["mapped"], 0)
                self.assertEqual(result["counts"]["missing"], 1)

    def test_unresolved_alias_does_not_match_by_displayed_team(self):
        result = match([fixture()], [titan(home_id=None)])
        self.assertEqual(result["counts"]["mapped"], 0)
        self.assertIn("canonical_home_away_missing_or_invalid", result["candidate_diagnostics"][0]["issues"])

    def test_naive_or_invalid_times_are_never_assigned_timezone(self):
        for time in ("2026-10-31T15:00:00", None, "bad date"):
            for expected, candidate in ((fixture(kickoff=time), titan()), (fixture(), titan(kickoff=time))):
                with self.subTest(time=time, expected=expected):
                    self.assertEqual(match([expected], [candidate])["counts"]["mapped"], 0)

    def test_invalid_source_identity_and_problem_flags_block_mapping(self):
        bad = [fixture(source_event_id="101"), fixture(provider="other"),
               fixture(problems=["identity conflict"]), fixture(status="postponed"),
               fixture(status="cancelled"), fixture(status="unknown")]
        for row in bad:
            with self.subTest(row=row):
                self.assertEqual(match([row], [titan()])["counts"]["missing"], 1)

    def test_completed_and_live_fixtures_can_be_identity_candidates_only(self):
        for state in ("completed", "live"):
            result = match([fixture(status=state)], [titan()])
            self.assertEqual(result["counts"]["mapped"], 1)
            self.assertFalse(result["formal_observation"])

    def test_empty_scope_and_invalid_rows_are_explicit(self):
        self.assertEqual(match([], [titan()])["counts"]["unused_candidate_rows"], 1)
        result = match([None, {}], [None])
        self.assertEqual(result["counts"]["missing"], 2)
        self.assertEqual(len(result["coverage"]), 2)

    def test_scope_inputs_are_not_mutated(self):
        expected, candidates = [fixture()], [titan()]
        before = copy.deepcopy((expected, candidates))
        match(expected, candidates)
        self.assertEqual((expected, candidates), before)

    def test_explicit_numeric_league_contract_required(self):
        for league in (None, "", True, " 36", "england-premier"):
            with self.subTest(league=league), self.assertRaises(ValueError):
                map_fixture_candidates([fixture()], [titan()], titan_league_id=league)

    def test_counts_partition_every_expected_row(self):
        result = match([fixture(), fixture("101", "Liverpool", "Everton"),
                        fixture("102", "Ipswich", "Coventry")],
                       [titan(), titan(home="Ipswich", away="Coventry", event="3004002"),
                        titan(home="Ipswich", away="Coventry", event="3004003")])
        counts = result["counts"]
        self.assertEqual((counts["mapped"], counts["missing"], counts["ambiguous"]), (1, 1, 1))
        self.assertEqual(counts["expected"], sum(counts[key] for key in ("mapped", "missing", "ambiguous")))


if __name__ == "__main__":
    unittest.main()
