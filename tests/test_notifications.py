import unittest

from run import _failure_signature


class FailureSignatureTests(unittest.TestCase):
    def test_same_http_failure_ignores_changing_query_timestamp(self):
        first = {
            "jobopportunities": (
                "HTTPError: 401 Client Error: Unauthorized for url: "
                "https://api.jobopportunitiesapi.org/public/jobs?title=SQL+Developer&"
                "posted_after=2026-09-30T17%3A03%3A04%2B00%3A00"
            )
        }
        second = {
            "jobopportunities": (
                "HTTPError: 401 Client Error: Unauthorized for url: "
                "https://api.jobopportunitiesapi.org/public/jobs?title=SQL+Developer&"
                "posted_after=2026-10-01T22%3A44%3A03%2B00%3A00"
            )
        }
        self.assertEqual(_failure_signature(first), _failure_signature(second))

    def test_different_http_status_is_a_new_failure_signature(self):
        unauthorized = {
            "jobopportunities": "HTTPError: 401 Client Error: Unauthorized for url: https://example.test/jobs"
        }
        forbidden = {
            "jobopportunities": "HTTPError: 403 Client Error: Forbidden for url: https://example.test/jobs"
        }
        self.assertNotEqual(_failure_signature(unauthorized), _failure_signature(forbidden))

    def test_same_http_status_on_different_sources_is_distinct(self):
        first = {"jobopportunities": "HTTPError: 401 Client Error: Unauthorized for url: https://example.test/jobs"}
        second = {"jobicy": "HTTPError: 401 Client Error: Unauthorized for url: https://example.test/jobs"}
        self.assertNotEqual(_failure_signature(first), _failure_signature(second))


if __name__ == "__main__":
    unittest.main()
