import unittest

from src import router


class RouterAutoReviewTest(unittest.TestCase):
    def test_repo_policy_can_keep_auto_review_in_triage(self):
        self.assertEqual(router._initial_status("zigbang/product-hub", True), "triage")

    def test_auto_review_still_applies_to_other_repos(self):
        self.assertEqual(router._initial_status("zigbang/zigbang-client", True), "intake")


if __name__ == "__main__":
    unittest.main()
