import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from univia.generate import build_request, recommend, validate_response
from univia.ingest import consolidate, normalize, save_dataset
from univia.retrieve import eligibility, retrieve
from univia.schema import ValidationError, validate_profile, validate_program


def program(pid="P1", **changes):
    p = dict(program_id=pid, university="Fictional Test University", program_name="Economics and Data",
             country="Italy", city="Test city", degree_level="bachelor", fields_of_study=["economics", "data"],
             teaching_language=["English"], full_program_languages=["English"], tuition_fee=6000,
             tuition_currency="EUR", fee_period="annual", estimated_living_cost=9000,
             living_cost_currency="EUR", living_cost_period="annual", annual_additional_costs=500,
             source_url="https://example.org/fees", living_cost_source_url="https://example.org/living",
             last_verified_date=date.today().isoformat(), costs_verified=True, requirements_verified=True,
             minimum_grades=12, minimum_grade_scale=20, required_qualifications=["school_diploma"],
             campus_type="urban", location_type="city")
    p.update(changes)
    return validate_program(p)


def student(**changes):
    s = dict(student_id="S001", interests=["economics", "data"], desired_degree_level="bachelor",
             grades={"average": 15, "scale": 20}, previous_qualification="school_diploma",
             annual_total_budget=18000, currency="EUR",
             hard_constraints={"eligible_countries": ["Italy"], "teaching_languages": ["English"], "require_verified_requirements": True})
    s.update(changes)
    return validate_profile(s)


class EligibilityTests(unittest.TestCase):
    def test_verified_affordable_program_passes(self):
        self.assertEqual(eligibility(program(), student()), [])

    def test_budget_includes_living_and_additional_costs(self):
        self.assertTrue(eligibility(program(estimated_living_cost=13000), student()))
        self.assertEqual(eligibility(program(), student(annual_total_budget=15500)), [])
        self.assertTrue(eligibility(program(), student(annual_total_budget=15499)))

    def test_missing_costs_currency_and_units_fail_closed(self):
        for changes in ({"estimated_living_cost": None}, {"annual_additional_costs": None}, {"costs_verified": None},
                        {"fee_period": "monthly"}, {"living_cost_period": "monthly"}, {"tuition_currency": "ZAR"},
                        {"living_cost_source_url": None}, {"source_url": None}, {"last_verified_date": "2000-01-01"}):
            with self.subTest(changes=changes):
                self.assertTrue(eligibility(program(**changes), student()))

    def test_language_mentions_do_not_prove_full_track(self):
        p = program(teaching_language=["English", "Spanish"], full_program_languages=None)
        self.assertTrue(eligibility(p, student()))

    def test_hard_country_degree_campus_and_exclusions(self):
        self.assertTrue(eligibility(program(country="Spain"), student()))
        self.assertTrue(eligibility(program(degree_level=None), student()))
        self.assertTrue(eligibility(program(), student(excluded_countries=["ITALY"])))
        s = student(hard_constraints={"campus_types": ["urban"]})
        self.assertTrue(eligibility(program(campus_type=None), s))

    def test_preferences_are_soft(self):
        self.assertFalse(eligibility(program(), student(preferred_countries=["France"], preferred_languages=["French"])))

    def test_requirements_do_not_use_inferred_equivalence(self):
        self.assertTrue(eligibility(program(), student(grades={"average": 90, "scale": 100})))
        self.assertTrue(eligibility(program(), student(grades={"average": 10, "scale": 20})))
        self.assertTrue(eligibility(program(), student(previous_qualification="different")))
        self.assertTrue(eligibility(program(admission_requirements=["IELTS 7 and an interview"]), student()))
        self.assertTrue(eligibility(program(required_qualifications=None), student()))

    def test_profile_invalid_and_unknown_constraints_rejected(self):
        for update in ({"hard_constraints": {"ignore_budget": True}}, {"hard_constraints": {"eligible_countries": []}},
                       {"annual_total_budget": -1}, {"annual_total_budget": float("nan")},
                       {"grades": {"average": 30, "scale": 20}}, {"interests": "economics"}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                student(**update)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        save_dataset([program(), program("P2", country="Spain", program_name="Economics in Spain")], self.root)
        self.r = retrieve(self.root / "programs.sqlite", student())

    def tearDown(self):
        self.tmp.cleanup()

    def test_graph_ranking_and_hard_filters(self):
        self.assertEqual(self.r["eligible_count"], 1)
        self.assertEqual(self.r["excluded"][0]["program_id"], "P2")
        reasons = self.r["candidates"][0]["allowed_reasons"]
        self.assertTrue(any(r["graph_paths"] for r in reasons))

    def test_prompt_only_contains_retrieved_records(self):
        body = build_request(self.r, "test-model")
        data = json.loads(body["messages"][1]["content"])
        self.assertEqual([c["program_id"] for c in data["candidates"]], ["P1"])
        self.assertNotIn("excluded", data)
        self.assertNotIn("raw_record", data["candidates"][0]["record"])
        self.assertEqual(body["response_format"]["type"], "json_schema")

    def test_unknown_unretrieved_duplicate_and_claims_rejected(self):
        responses = [
            {"recommendations": [{"program_id": "P2", "reason_ids": ["R1"]}]},
            {"recommendations": [{"program_id": "FAKE", "reason_ids": ["R1"]}]},
            {"recommendations": [{"program_id": "P1", "reason_ids": ["invented"]}]},
            {"recommendations": [{"program_id": "P1", "reason_ids": ["R1"], "tuition": 0}]},
            {"recommendations": [], "explanation": "All courses are free"},
            {"recommendations": [{"program_id": "P1", "reason_ids": ["R1", "R1"]}]},
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValidationError):
                validate_response(response, self.r)

    def test_duplicate_program_rejected(self):
        r = retrieve(self.root / "programs.sqlite", student(hard_constraints={}))
        row = {"program_id": "P1", "reason_ids": ["R1"]}
        with self.assertRaises(ValidationError):
            validate_response({"recommendations": [row, row]}, r)

    def test_final_validation_rechecks_constraints(self):
        r = copy.deepcopy(self.r)
        r["candidates"][0]["record"]["tuition_fee"] = 99999
        with self.assertRaises(ValidationError):
            validate_response({"recommendations": [{"program_id": "P1", "reason_ids": ["R1"]}]}, r)

    def test_mock_mistral_and_offline_hydrate_database_facts(self):
        def transport(body):
            self.assertEqual(body["model"], "test-model")
            return {"recommendations": [{"program_id": "P1", "reason_ids": ["R1"]}]}
        result = recommend(self.r, model="test-model", transport=transport)
        self.assertEqual(result["mode"], "mistral")
        self.assertEqual(result["recommendations"][0]["program"]["tuition_fee"], 6000)
        self.assertEqual(recommend(self.r, offline=True)["mode"], "offline_retrieval")

    def test_no_candidates_makes_no_api_call(self):
        r = retrieve(self.root / "programs.sqlite", student(excluded_countries=["Italy", "Spain"]))
        def forbidden(_):
            self.fail("No API call should be made")
        self.assertEqual(recommend(r, transport=forbidden)["status"], "no_eligible_programs")

    def test_http_request_and_envelope_validation(self):
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"recommendations": [{"program_id": "P1", "reason_ids": ["R1"]}]})}}]}
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "fictional-test-key"}), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(envelope).encode())) as send:
            result = recommend(self.r)
            request = send.call_args[0][0]
            self.assertEqual(request.full_url, "https://api.mistral.ai/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer fictional-test-key")
            body = json.loads(request.data)
            self.assertIn("schema", body["response_format"]["json_schema"])
            self.assertEqual(result["recommendations"][0]["program_id"], "P1")

    def test_missing_key_and_truncated_response_rejected(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValidationError):
            recommend(self.r)
        envelope = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "fictional-test-key"}), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(envelope).encode())), self.assertRaises(ValidationError):
            recommend(self.r)

    def test_top_k_bounds(self):
        for k in (0, 4, 11, True):
            with self.assertRaises(ValidationError):
                retrieve(self.root / "programs.sqlite", student(), k)

    def test_csv_round_trip(self):
        programs, errors = consolidate([self.root / "programs.csv"])
        self.assertFalse(errors)
        self.assertEqual(programs[0]["costs_verified"], True)
        self.assertEqual(programs[0]["fields_of_study"], ["economics", "data"])

    def test_dedup_and_conflict_reporting(self):
        source = self.root / "input.json"
        source.write_text(json.dumps([program(), program()]))
        programs, rejected = consolidate([source])
        self.assertEqual(len(programs), 1)
        self.assertFalse(rejected)
        source.write_text(json.dumps([program(), program(tuition_fee=100)]))
        _, rejected = consolidate([source])
        self.assertEqual(len(rejected), 1)

    def test_xlsx_import_preserves_specializations_and_uncertainty(self):
        workbook = Path(__file__).resolve().parents[1] / "UNIVIA.xlsx"
        if not workbook.exists():
            self.skipTest("Original workbook not present")
        programs, errors = consolidate([workbook])
        self.assertFalse(errors)
        self.assertEqual(len(programs), 87)
        bcom = [p for p in programs if p["program_name"] == "BCom"]
        self.assertEqual(len(bcom), 12)
        self.assertEqual(len({p["program_id"] for p in bcom}), 12)
        monthly = [p for p in programs if p["fee_period"] == "monthly"]
        self.assertEqual(len(monthly), 3)
        self.assertTrue(all(p["tuition_fee"] == 780 for p in monthly))
        self.assertTrue(all(p["estimated_living_cost"] is None for p in programs))
        self.assertTrue(all(p["degree_level"] is None for p in programs))


if __name__ == "__main__":
    unittest.main()
