import unittest

from app.main import build_service


class InvoiceTests(unittest.TestCase):
    def test_total_due_counts_open_only(self):
        svc = build_service()
        a = svc.create("a", 100)
        svc.create("b", 50)
        svc.pay(a["id"])
        self.assertEqual(svc.total_due(), 50)


if __name__ == "__main__":
    unittest.main()
