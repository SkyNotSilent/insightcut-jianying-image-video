import unittest
from check_contribution import forbidden_path


class ContributionTests(unittest.TestCase):
    def test_private_variants(self):
        for name in ['.env', 'service/.env.production', '.env.development', 'data/local.db']:
            self.assertTrue(forbidden_path(name), name)

    def test_examples_and_sources(self):
        for name in ['.env.example', 'service/.env.production.example', 'src/config.py']:
            self.assertFalse(forbidden_path(name), name)


if __name__ == '__main__':
    unittest.main()
