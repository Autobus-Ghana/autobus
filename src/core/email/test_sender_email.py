import unittest

from core.email.sender_email import SenderEmailInvalid, normalize_sender_email


class NormalizeSenderEmailTests(unittest.TestCase):
    def test_business_domain_address(self):
        self.assertEqual(
            normalize_sender_email("noreply@usegreenmall.com"),
            "noreply@usegreenmall.com",
        )

    def test_dotted_shorthand_uses_the_business_domain(self):
        self.assertEqual(
            normalize_sender_email("noreply.usegreenmall.com"),
            "noreply@usegreenmall.com",
        )

    def test_autobus_shorthand_still_works(self):
        self.assertEqual(
            normalize_sender_email("noreply.useautobus.com"),
            "noreply@useautobus.com",
        )

    def test_bare_name_uses_the_default_domain(self):
        self.assertEqual(normalize_sender_email("noreply"), "noreply@useautobus.com")

    def test_host_is_lowercased(self):
        self.assertEqual(
            normalize_sender_email("Hello@UseGreenMall.com"),
            "Hello@usegreenmall.com",
        )

    def test_rejects_a_domain_without_a_dot(self):
        with self.assertRaises(SenderEmailInvalid):
            normalize_sender_email("noreply@localhost")


if __name__ == "__main__":
    unittest.main()
