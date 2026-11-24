import unittest
from pathlib import Path

from univia.ingest import ALIASES, audit_workbooks, consolidate
from univia.schema import clean


class WorkbookAuditTests(unittest.TestCase):
    def test_every_workbook_sheet_row_and_original_cell_is_preserved(self):
        path = Path(__file__).resolve().parents[1] / 'UNIVIA.xlsx'
        if not path.exists():
            self.skipTest('Legacy workbook not present')
        programs, rejected = consolidate([path])
        self.assertFalse(rejected)
        audit = audit_workbooks([path], programs)
        self.assertTrue(audit['all_program_rows_imported'])
        self.assertEqual(audit['program_count'], 87)
        book = audit['workbooks'][0]
        self.assertEqual(book['sheet_count'], 2)
        sheets = {s['sheet']: s for s in book['sheets']}
        self.assertEqual(sheets['Healthcare']['program_row_count'], 42)
        self.assertEqual(sheets['Portugal and South Africa']['program_row_count'], 45)
        self.assertEqual(audit['countries'], ['Cyprus', 'Portugal', 'South Africa', 'Spain'])
        for p in programs:
            source = p['provenance'][-1]
            sheet = sheets[source['sheet']]
            for ref, value in p['raw_record']['_excel_cells'].items():
                self.assertEqual(sheet['cells'][ref]['value'], value)
            # Every populated named input field has a canonical destination.
            for name, value in p['raw_record'].items():
                if name.startswith('_') or clean(value) is None:
                    continue
                target = ALIASES.get(name, name.replace(' ', '_'))
                self.assertIn(target, p)
                self.assertIsNotNone(p[target], (source, name, value))
        # Original missing-value statements remain in raw evidence, not replaced.
        record = next(p for p in programs if p['country'] == 'South Africa')
        self.assertEqual(record['raw_record']['teaching language'], 'Not verified')
        self.assertIsNone(record['teaching_language_notes'])


if __name__ == '__main__':
    unittest.main()
