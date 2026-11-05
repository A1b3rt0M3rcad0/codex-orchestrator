import unittest
from textutil import normalize_label


class LabelContract(unittest.TestCase):
    def test_whitespace(self):
        self.assertEqual(normalize_label("  Alpha\t Beta\n"), "Alpha Beta")
        self.assertEqual(normalize_label(""), "")
        self.assertEqual(normalize_label("   "), "")

    def test_unicode_and_case_are_preserved(self):
        self.assertEqual(normalize_label("  Ação   Útil  "), "Ação Útil")

    def test_type_boundary(self):
        for value in (None, 1, [], b"text"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                normalize_label(value)


if __name__ == "__main__":
    unittest.main()
