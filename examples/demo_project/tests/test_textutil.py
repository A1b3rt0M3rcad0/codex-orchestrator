import unittest
from textutil import normalize_name


class NormalizeNameTests(unittest.TestCase):
    def test_whitespace(self):
        self.assertEqual(normalize_name("  Ada\t  Lovelace\n"), "Ada Lovelace")

    def test_empty(self):
        self.assertEqual(normalize_name(""), "")

    def test_invalid_type(self):
        with self.assertRaises(TypeError):
            normalize_name(None)


if __name__ == "__main__":
    unittest.main()
