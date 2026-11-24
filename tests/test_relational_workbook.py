import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from univia.generate import build_request
from univia.ingest import audit_workbooks, consolidate, save_dataset
from univia.questions import answer_question
from univia.retrieve import retrieve
from univia.workbook import archive_rows, read_tables


class RelationalWorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).resolve().parents[1] / 'UNIVIA_INTL_DB_v4_SG_final.xlsx'
        if not cls.path.exists():
            raise unittest.SkipTest('Full workbook not present')
        cls.programs, cls.errors = consolidate([cls.path])
        cls.tables = read_tables(cls.path)
        cls.rows = archive_rows([cls.path])
        cls.tmp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.tmp.name) / 'programs.sqlite'
        save_dataset(cls.programs, cls.tmp.name, workbook_rows=cls.rows)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_programs_and_countries_are_imported(self):
        self.assertFalse(self.errors)
        self.assertEqual(len(self.programs), 99)
        self.assertEqual(len({p['program_id'] for p in self.programs}), 99)
        self.assertEqual(Counter(p['country'] for p in self.programs), {'Spain': 38, 'Cyprus': 4, 'Portugal': 8, 'South Africa': 37, 'Romania': 12})
        self.assertEqual(len(self.tables), 22)
        self.assertEqual(len(self.tables['Countries']), 8)

    def test_every_table_row_and_original_cell_survives_sqlite_and_json(self):
        audit = audit_workbooks([self.path], self.programs)
        self.assertTrue(audit['all_program_rows_imported'])
        self.assertEqual(audit['workbooks'][0]['sheet_count'], 22)
        archived = json.loads((self.database.parent / 'workbook_tables.json').read_text())
        self.assertEqual(len(archived), 461)
        sheets = {s['sheet']: s for s in audit['workbooks'][0]['sheets']}
        with sqlite3.connect(self.database) as db:
            rows = db.execute('SELECT table_name,record FROM workbook_rows').fetchall()
            self.assertEqual(len(rows), 461)
            self.assertEqual(Counter(table for table, _ in rows), {table: len(records) for table, records in self.tables.items() if records})
            for table, text in rows:
                row = json.loads(text)
                for ref, value in row['_excel_cells'].items():
                    self.assertEqual(sheets[table]['cells'][ref]['value'], value)
        self.assertEqual(len(self.tables['Sources']), 136)
        for table in ('Prompts', 'FX_Rates', 'Scripts', 'Tags', 'Testimonies', 'Domains'):
            self.assertEqual(self.tables[table], [])
            self.assertTrue(sheets[table]['cells'])  # Even empty-table headers are archived.

    def test_relationships_and_sources_are_joined_without_guessing(self):
        p = next(p for p in self.programs if p['program_id'] == 'RO-UMFIASI-MED')
        self.assertEqual(p['country'], 'Romania')
        self.assertEqual(p['tuition_fee'], 9000)
        self.assertEqual(p['fee_period'], 'annual')
        self.assertEqual(p['full_program_languages'], ['English'])
        self.assertEqual(p['related_records']['Programmes'][0]['prog_id'], p['program_id'])
        self.assertEqual(p['related_records']['Countries'][0]['country_id'], 'RO')
        for table in ('Institutions', 'Cities', 'Regions', 'Admission_Requirements', 'Application_Routes', 'Pathways', 'Scholarships'):
            self.assertTrue(p['related_records'][table], table)
        self.assertTrue(p['source_url'].startswith('https://'))
        self.assertTrue(p['field_sources']['tuition_eu'][0]['quote'])
        self.assertTrue(p['source_records'])
        self.assertIsNone(p['minimum_grades'])  # No automatic grade-scale/alternative-rule conversion.
        self.assertIsNone(p['degree_level'])
        self.assertIsNone(p['estimated_living_cost'])  # A country range is not a program quote.

    def test_fee_units_are_not_conflated(self):
        for p in self.programs:
            raw = p['raw_record']
            if raw['tuition_unit'] == 'ECTS':
                self.assertEqual(p['fee_period'], 'ects')
                self.assertEqual(p['tuition_fee'], float(raw['tuition_eu']))
            elif raw['tuition_unit'] == 'first-year total':
                self.assertEqual(p['first_year_academic_total'], float(raw['tuition_eu']))
                self.assertIsNone(p['tuition_fee'])
            elif raw['tuition_unit'] == 'month':
                self.assertEqual(p['fee_period'], 'monthly')
                self.assertEqual(p['tuition_fee'], 780)

    def test_romania_cheapest_and_source_graph_are_searchable(self):
        result = answer_question(self.database, 'I want to study medicine in Romania. Give me the cheapest option', offline=True)
        self.assertEqual(len(result['recommendations']), 1)
        p = result['recommendations'][0]['program']
        self.assertEqual(p['country'], 'Romania')
        self.assertEqual(p['fields_of_study'], ['Medicine'])
        self.assertEqual(p['tuition_fee'], 9000)
        self.assertEqual(p['fee_period'], 'annual')
        with sqlite3.connect(self.database) as db:
            self.assertTrue(db.execute("SELECT 1 FROM edges WHERE source=? AND relation='supported_by'", (p['program_id'],)).fetchone())
            self.assertTrue(db.execute("SELECT 1 FROM edges WHERE source=? AND relation='has_admission_requirements'", (p['program_id'],)).fetchone())

    def test_country_only_records_remain_accessible(self):
        result = answer_question(self.database, 'Medicine in Belgium', offline=True)
        self.assertFalse(result['recommendations'])
        self.assertEqual(result['country_information'][0]['name_en'], 'Belgium')

    def test_mistral_context_has_source_evidence_without_excel_cache(self):
        r = retrieve(self.database, {'student_id': 'TEST', 'interests': ['medicine'], 'expectations_summary': 'Medicine in Romania and admission requirements', 'hard_constraints': {'eligible_countries': ['Romania']}}, include_unverified=True)
        request = build_request(r, 'test-model')
        payload = json.loads(request['messages'][1]['content'])
        self.assertLessEqual(len(payload['candidates']), 10)
        for c in payload['candidates']:
            self.assertEqual(c['record']['country'], 'Romania')
            self.assertTrue(c['record']['sources'])
            self.assertTrue(c['record']['related_records']['Admission_Requirements'])
            self.assertNotIn('_excel_cells', json.dumps(c))
            self.assertNotIn('_provenance', json.dumps(c))
            self.assertNotIn('raw_record', c['record'])


if __name__ == '__main__':
    unittest.main()
