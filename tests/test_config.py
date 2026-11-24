import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from univia.config import load_env
from univia.schema import ValidationError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / '.env'
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_missing_file_is_optional(self):
        load_env(self.path)
        self.assertNotIn('MISTRAL_API_KEY', os.environ)

    def test_load_key_quotes_comments_and_empty_values(self):
        self.path.write_text('# Local test\nMISTRAL_API_KEY="fictional#key" # comment\nexport MISTRAL_MODEL=example-model\nEMPTY=\n')
        load_env(self.path)
        self.assertEqual(os.environ['MISTRAL_API_KEY'], 'fictional#key')
        self.assertEqual(os.environ['MISTRAL_MODEL'], 'example-model')
        self.assertEqual(os.environ['EMPTY'], '')

    def test_existing_environment_takes_priority(self):
        os.environ['MISTRAL_API_KEY'] = 'existing-test-key'
        self.path.write_text('MISTRAL_API_KEY=file-test-key\n')
        load_env(self.path)
        self.assertEqual(os.environ['MISTRAL_API_KEY'], 'existing-test-key')

    def test_bad_config_does_not_expose_value_or_partially_load(self):
        self.path.write_text('FIRST=value\nMISTRAL_API_KEY="fictional-secret\n')
        with self.assertRaises(ValidationError) as caught:
            load_env(self.path)
        self.assertNotIn('fictional-secret', str(caught.exception))
        self.assertNotIn('FIRST', os.environ)

    def test_startup_loads_local_configuration(self):
        from univia.__main__ import main
        with patch('sys.argv', ['univia', 'serve']), patch('univia.__main__.load_env') as load, patch('univia.server.serve') as serve:
            self.assertEqual(main(), 0)
            load.assert_called_once_with()
            serve.assert_called_once_with('data/programs.sqlite', 8000)


if __name__ == '__main__':
    unittest.main()
