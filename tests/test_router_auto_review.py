import unittest

from src import router


class RouterAutoReviewTest(unittest.TestCase):
    def test_repo_policy_can_keep_global_auto_review_in_triage(self):
        old_all = router.AUTO_REVIEW_ALL
        old_authors = router.AUTO_REVIEW_AUTHORS
        try:
            router.AUTO_REVIEW_ALL = True
            router.AUTO_REVIEW_AUTHORS = {"*"}
            self.assertEqual(router._initial_status("zigbang/product-hub", "anyone"), "triage")
        finally:
            router.AUTO_REVIEW_ALL = old_all
            router.AUTO_REVIEW_AUTHORS = old_authors

    def test_global_auto_review_still_applies_to_other_repos(self):
        old_all = router.AUTO_REVIEW_ALL
        old_authors = router.AUTO_REVIEW_AUTHORS
        try:
            router.AUTO_REVIEW_ALL = True
            router.AUTO_REVIEW_AUTHORS = {"*"}
            self.assertEqual(router._initial_status("zigbang/zigbang-client", "anyone"), "intake")
        finally:
            router.AUTO_REVIEW_ALL = old_all
            router.AUTO_REVIEW_AUTHORS = old_authors


if __name__ == "__main__":
    unittest.main()