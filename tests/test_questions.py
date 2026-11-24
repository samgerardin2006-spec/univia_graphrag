import json
import tempfile
import unittest
import io
import os
import urllib.error
from pathlib import Path
from unittest.mock import patch

from univia.countries import COUNTRIES
from univia.ingest import save_dataset
from univia.questions import answer_question, parse_question
from univia.schema import ValidationError, validate_program


class QuestionTests(unittest.TestCase):
    def test_country_names_and_aliases_are_hard_filters_without_database_coverage(self):
        for country, aliases in COUNTRIES.items():
            for alias in aliases:
                with self.subTest(country=country, alias=alias):
                    parsed = parse_question('Medicine in ' + alias)
                    self.assertEqual(parsed['profile']['hard_constraints']['eligible_countries'], [country])
                    self.assertEqual(parsed['profile']['interests'], ['medicine'])

    def test_romania_request_never_returns_other_countries(self):
        with tempfile.TemporaryDirectory() as directory:
            def record(pid, country):
                return validate_program(dict(program_id=pid, university='Fictional Test University', program_name='Medicine', country=country))

            database = Path(directory) / 'programs.sqlite'
            question = 'I would like to do medicine in Romania'
            save_dataset([record('CY', 'Cyprus'), record('ES', 'Spain')], directory)

            def transport(body):
                data = json.loads(body['messages'][1]['content'])
                self.assertEqual(data['student_profile']['hard_constraints']['eligible_countries'], ['Romania'])
                self.assertEqual(data['candidates'], [])
                return {'recommendations': []}

            for options in ({'offline': True}, {'transport': transport}):
                with self.subTest(options=options):
                    result = answer_question(database, question, **options)
                    self.assertEqual(result['recommendations'], [])
                    self.assertIn('Countries: Romania', result['understood'])
                    self.assertIn('workbook contains programs', result['answer'])

            save_dataset([record('RO', 'Romania'), record('CY', 'Cyprus')], directory)
            result = answer_question(database, question, offline=True)
            self.assertEqual([r['program']['country'] for r in result['recommendations']], ['Romania'])

    def test_new_country_names_support_exclusions_and_multiple_destinations(self):
        p = parse_question('Medicine in Romania or Bulgaria except Hungary')['profile']
        self.assertEqual(p['hard_constraints']['eligible_countries'], ['Romania', 'Bulgaria'])
        self.assertEqual(p['excluded_countries'], ['Hungary'])
        p = parse_question('Medicine excluding Romania and Bulgaria')['profile']
        self.assertNotIn('eligible_countries', p['hard_constraints'])
        self.assertEqual(p['excluded_countries'], ['Romania', 'Bulgaria'])

    def test_plain_question_becomes_profile(self):
        parsed = parse_question("I'm interested in economics and data. What could I study in Portugal?")
        self.assertFalse(parsed['clarifications'])
        self.assertEqual(parsed['profile']['hard_constraints']['eligible_countries'], ['Portugal'])
        self.assertIn('economics', parsed['profile']['interests'])
        self.assertIn('data', parsed['profile']['interests'])

    def test_degree_language_and_budget_are_hard(self):
        p = parse_question('An English bachelor in Italy with a total budget under EUR 18,000 per year')['profile']
        self.assertEqual(p['desired_degree_level'], 'bachelor')
        self.assertEqual(p['hard_constraints']['teaching_languages'], ['English'])
        self.assertEqual(p['annual_total_budget'], 18000)
        self.assertEqual(p['currency'], 'EUR')

    def test_currency_formats(self):
        for value in ('€18k', '18,000 EUR', 'EUR 18000', '18 000 euros'):
            with self.subTest(value=value):
                p = parse_question('Medicine with a yearly budget of ' + value)['profile']
                self.assertEqual(p['annual_total_budget'], 18000)
                self.assertEqual(p['currency'], 'EUR')

    def test_exclusions_are_not_positive_filters(self):
        for phrase in ('except Spain and Cyprus', 'not in Spain and Cyprus', 'excluding Spain, Cyprus'):
            with self.subTest(phrase=phrase):
                p = parse_question('Economics ' + phrase)['profile']
                self.assertEqual(set(p['excluded_countries']), {'Spain', 'Cyprus'})
                self.assertNotIn('eligible_countries', p['hard_constraints'])

    def test_ambiguous_or_unsupported_requirements_ask_for_clarification(self):
        for question in ('Economics with a budget of 18000',
                         'Medicine with a monthly budget of EUR 1000',
                         'Medicine for EUR 18000', 'Economics with a required scholarship',
                         'Am I eligible for medicine?', 'Economics in Europe', 'Economics near Lisbon',
                         'Economics not taught in English', 'A bachelor or master in economics'):
            with self.subTest(question=question):
                self.assertTrue(parse_question(question)['clarifications'])

    def test_question_input_validation(self):
        for question in (None, 12, '', 'a', 'x' * 2001):
            with self.assertRaises(ValidationError):
                parse_question(question)

    def test_answer_uses_only_retrieved_records(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = [validate_program(dict(program_id=pid, university='Fictional Test University', program_name=name, country=country, fields_of_study=[subject])) for pid, name, country, subject in [('P1', 'Economics', 'Portugal', 'economics'), ('P2', 'Economics', 'Spain', 'economics'), ('P3', 'Medicine', 'Portugal', 'medicine')]]
            save_dataset(programs, directory)
            database = Path(directory) / 'programs.sqlite'
            def transport(body):
                data = json.loads(body['messages'][1]['content'])
                self.assertEqual([c['program_id'] for c in data['candidates']], ['P1'])
                self.assertIn('Economics in Portugal', data['student_profile']['expectations_summary'])
                return {'recommendations': [{'program_id': 'P1', 'reason_ids': ['R1']}]}
            result = answer_question(database, 'Economics in Portugal', transport=transport)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['recommendations'][0]['program_id'], 'P1')
            self.assertTrue(result['answer'])
            result = answer_question(database, 'Physics in Portugal', offline=True)
            self.assertEqual(result['status'], 'no_eligible_programs')
            self.assertFalse(result['recommendations'])

    def test_incomplete_requirements_still_get_answer_and_flagged_leads(self):
        with tempfile.TemporaryDirectory() as directory:
            save_dataset([validate_program(dict(program_id='P1', university='Fictional Test University', program_name='Economics', country='Portugal'))], directory)
            result = answer_question(Path(directory) / 'programs.sqlite', 'Economics with a mandatory scholarship', offline=True)
            self.assertTrue(result['answer'])
            self.assertEqual(result['recommendations'][0]['match_status'], 'unverified')
            self.assertTrue(result['recommendations'][0]['pending_checks'])

    def test_empty_answer_explains_missing_evidence_without_relaxing(self):
        with tempfile.TemporaryDirectory() as directory:
            save_dataset([validate_program(dict(program_id='P1', university='Fictional Test University', program_name='Economics', country='Portugal'))], directory)
            result = answer_question(Path(directory) / 'programs.sqlite', 'English bachelor in Portugal under EUR 18000 yearly', offline=True)
            self.assertEqual(result['status'], 'ok')
            self.assertNotIn('unverified', result['answer'].lower())
            self.assertEqual(result['recommendations'][0]['validation'], 'evidence_checked_eligibility_unverified')

    def test_cheapest_is_not_a_missing_budget_and_checks_all_candidates(self):
        q = parse_question('I want to study medicine in Romania. Give me the cheapest option')
        self.assertTrue(q['cheapest'])
        self.assertIsNone(q['pending_budget'])
        self.assertFalse(q['clarifications'])
        self.assertEqual(q['profile']['interests'], ['medicine'])
        with tempfile.TemporaryDirectory() as directory:
            records = [validate_program(dict(program_id=f'P{i:02}', university='Fictional Test University', program_name='Medicine', country='Romania', tuition_fee=20000-i*1000, tuition_currency='EUR', fee_period='annual')) for i in range(12)]
            records.append(validate_program(dict(program_id='WRONG', university='Fictional Test University', program_name='Economics', country='Romania', tuition_fee=1, tuition_currency='EUR', fee_period='annual')))
            save_dataset(records, directory)
            result = answer_question(Path(directory) / 'programs.sqlite', 'Medicine in Romania. Give me the cheapest option', offline=True)
            self.assertEqual([r['program_id'] for r in result['recommendations']], ['P11'])
            self.assertEqual(result['recommendations'][0]['program']['tuition_fee'], 9000)
            self.assertNotIn('unverified', result['answer'].lower())
            self.assertNotIn('budget', result['answer'].lower())

    def test_cheapest_never_compares_monthly_to_annual_as_same_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [validate_program(dict(program_id=pid, university='Fictional Test University', program_name='Medicine', country='Romania', tuition_fee=fee, tuition_currency='EUR', fee_period=period)) for pid, fee, period in [('MONTHLY', 780, 'monthly'), ('ANNUAL', 7000, 'annual'), ('EXPENSIVE', 9000, 'annual')]]
            save_dataset(records, directory)
            result = answer_question(Path(directory) / 'programs.sqlite', 'Cheapest medicine in Romania', offline=True)
            self.assertEqual({r['program_id'] for r in result['recommendations']}, {'MONTHLY', 'ANNUAL'})
            self.assertIn('grouped by currency and fee period', result['answer'])

    def test_screenshot_budget_and_region_are_preserved(self):
        question = "I want to study medicine in Eastern Europe, my budget is 10k for tuition only I don't care about living costs"
        p = parse_question(question)
        self.assertEqual(p['pending_budget'], {'amount': 10000, 'currency': None, 'scope': 'tuition'})
        self.assertNotIn('annual_total_budget', p['profile'])
        self.assertIn('Poland', p['profile']['hard_constraints']['eligible_countries'])
        self.assertNotIn('Spain', p['profile']['hard_constraints']['eligible_countries'])

    def test_tuition_budget_does_not_require_living_cost(self):
        from univia.retrieve import eligibility
        from datetime import date
        p = parse_question('Economics in Portugal with tuition under EUR 10000')['profile']
        self.assertEqual(p['annual_tuition_budget'], 10000)
        record = validate_program(dict(program_id='P1', university='Fictional Test University', program_name='Economics', country='Portugal', tuition_fee=9000, tuition_currency='EUR', fee_period='annual', source_url='https://example.org', last_verified_date=date.today().isoformat()))
        self.assertFalse(eligibility(record, p))
        record['tuition_fee'] = 11000
        self.assertTrue(eligibility(record, p))

    def test_known_violations_cannot_become_unverified_leads(self):
        from univia.retrieve import retrieve
        from univia.generate import validate_response
        with tempfile.TemporaryDirectory() as directory:
            records = [validate_program(dict(program_id=pid, university='Fictional Test University', program_name='Economics', country=country, tuition_fee=fee, tuition_currency='EUR', fee_period='annual')) for pid, country, fee in [('OVER', 'Portugal', 15000), ('OTHER', 'Spain', 5000), ('PENDING', 'Portugal', 5000)]]
            save_dataset(records, directory)
            profile = parse_question('Economics in Portugal with tuition under EUR 10000')['profile']
            retrieved = retrieve(Path(directory) / 'programs.sqlite', profile, include_unverified=True)
            self.assertEqual([c['program_id'] for c in retrieved['candidates']], ['PENDING'])
            retrieved['candidates'][0]['record']['tuition_fee'] = 20000
            with self.assertRaises(ValidationError):
                validate_response({'recommendations': [{'program_id': 'PENDING', 'reason_ids': ['R1']}]}, retrieved)

    def test_mistral_is_called_even_when_region_has_no_records(self):
        with tempfile.TemporaryDirectory() as directory:
            save_dataset([validate_program(dict(program_id='P1', university='Fictional Test University', program_name='Medicine', country='Spain'))], directory)
            envelope = {'id': 'test-request-id', 'model': 'test-model', 'usage': {'total_tokens': 99}, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"recommendations":[]}'}}]}
            with patch.dict(os.environ, {'MISTRAL_API_KEY': 'fictional-test-key'}), patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(envelope).encode())) as send:
                result = answer_question(Path(directory) / 'programs.sqlite', 'Medicine in Eastern Europe with a tuition budget of 10k')
                send.assert_called_once()
                self.assertEqual(result['api']['status'], 'success')
                self.assertEqual(result['api']['request_id'], 'test-request-id')
                self.assertTrue(result['api']['called'])
                self.assertEqual(result['recommendations'], [])
                self.assertIn('Spain', result['answer'])
                self.assertNotIn('fictional-test-key', json.dumps(result))

    def test_api_failure_keeps_answer_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            save_dataset([validate_program(dict(program_id='P1', university='Fictional Test University', program_name='Economics', country='Portugal'))], directory)
            with patch.dict(os.environ, {'MISTRAL_API_KEY': 'fictional-test-key'}), patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError('https://api.mistral.ai', 401, 'unauthorized', {}, None)):
                result = answer_question(Path(directory) / 'programs.sqlite', 'Economics in Portugal')
                self.assertEqual(result['api']['http_status'], 401)
                self.assertEqual(result['mode'], 'database_fallback')
                self.assertEqual(result['api']['status'], 'error')
                self.assertTrue(result['answer'])
                self.assertTrue(result['recommendations'])


if __name__ == '__main__':
    unittest.main()
