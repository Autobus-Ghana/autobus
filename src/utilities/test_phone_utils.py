"""Phone normalization used when saving a customer from a chat order."""

import unittest

from utilities.phone_utils import convert_to_local_ghana_format


class ConvertToLocalGhanaFormatTests(unittest.TestCase):
    def test_whatsapp_international(self):
        self.assertEqual(convert_to_local_ghana_format("233550748724"), "0550748724")
        self.assertEqual(convert_to_local_ghana_format("+233 55 074 8724"), "0550748724")

    def test_whatsapp_trunk_zero_after_country_code(self):
        self.assertEqual(convert_to_local_ghana_format("2330550748724"), "0550748724")
        self.assertEqual(convert_to_local_ghana_format("00233550748724"), "0550748724")

    def test_already_local(self):
        self.assertEqual(convert_to_local_ghana_format("0550748724"), "0550748724")


if __name__ == "__main__":
    unittest.main()
