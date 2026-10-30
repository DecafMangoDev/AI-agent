import unittest

from order import calculate_total

class OrderTest(unittest.TestCase):
    def test_discount(self) -> None:
        self.assertEqual(calculate_total([100, 50], 10), 135)
    
    def test_invalid_discount(self) -> None:
        with self.assertRaises(ValueError):
            calculate_total([100, 50], -10)