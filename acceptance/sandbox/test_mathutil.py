"""Gate test for the sandbox. Green on baseline; ticket-03's bad edit turns it red."""
import unittest

import mathutil


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(mathutil.add(2, 3), 5)

    def test_add_negative(self):
        self.assertEqual(mathutil.add(-1, -1), -2)


if __name__ == "__main__":
    unittest.main()
