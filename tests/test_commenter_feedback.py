import json
import unittest

from src import commenter


class Finding(dict):
    def __getitem__(self, key):
        return self.get(key)


def finding():
    return Finding(
        body=json.dumps({"problem": "문제", "fix": "제안"}, ensure_ascii=False),
        file="src/app.ts",
        line="10",
        title="제목",
        fp="repo#1:src/app.ts:10:rule",
    )


class CommenterFeedbackTest(unittest.TestCase):
    def test_code_footer_uses_code_reasons(self):
        body = commenter.render_bundle(
            "author", [finding()], subject="코드", feedback_subject="코드"
        )
        self.assertIn("이 코드 리뷰가 도움이 되었나요?", body)
        self.assertIn("오탐 / 맥락 부족 / 영향 과장 / 중복 / 톤", body)
        self.assertNotIn("사실 오류 / 맥락 부족 / 불명확", body)

    def test_doc_footer_uses_doc_reasons(self):
        body = commenter.render_bundle(
            "author", [finding()], subject="문서", feedback_subject="문서"
        )
        self.assertIn("이 문서 리뷰가 도움이 되었나요?", body)
        self.assertIn("사실 오류 / 맥락 부족 / 불명확 / 중복 / 톤", body)
        self.assertNotIn("오탐 / 맥락 부족 / 영향 과장", body)

    def test_feedback_subject_suppresses_existing_footer(self):
        posted = ["이 문서 리뷰가 도움이 되었나요? 이 댓글에 👍 / 👎 반응을 남겨주세요."]
        self.assertEqual(commenter._feedback_subject({"profile_type": "code"}, posted), "")
        self.assertEqual(commenter._feedback_subject({"profile_type": "doc"}, posted), "")

    def test_feedback_subject_defaults_by_profile(self):
        self.assertEqual(commenter._feedback_subject({"profile_type": "code"}, []), "코드")
        self.assertEqual(commenter._feedback_subject({"profile_type": "doc"}, []), "문서")


if __name__ == "__main__":
    unittest.main()
