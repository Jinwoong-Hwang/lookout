"""리뷰 프롬프트에 실리는 PR 대화를 어떻게 줄이는지에 대한 계약.

#10066 에서 두 번 데인 자리다. ⑴ 뒤에서 N자만 남기니 대화 127k자 중 살아남은
16k자가 전부 봇 코멘트라 작성자 해명이 사라졌고, ⑵ 작성자 글을 지키게 고쳤더니
이번엔 봇 지적 목록이 통째로 밀려나 같은 문제를 새 지문으로 다시 찾게 됐다.
"""
import json
import unittest

from src import ghclient

MARKER = "<!-- hermes:fp=owner/repo#1:src/app.ts:10:stale-token -->"
MARKER2 = "<!-- hermes:fp=owner/repo#1:src/b.ts:20:missing-guard -->"


class ClipBodyTest(unittest.TestCase):
    def test_short_body_is_untouched(self):
        self.assertEqual(ghclient._clip_body("짧은 본문", 100), "짧은 본문")

    def test_long_body_keeps_head_and_tail(self):
        """'보류' 표는 본문 끝에 붙는다 — 머리만 남기면 그게 먼저 잘린다."""
        body = "머리" * 100 + "X" * 500 + "보류 항목"
        out = ghclient._clip_body(body, 200)
        self.assertLess(len(out), len(body))
        self.assertTrue(out.startswith("머리머리"))
        self.assertTrue(out.endswith("보류 항목"))
        self.assertIn("본문 중략", out)


class CompactFindingsTest(unittest.TestCase):
    def test_each_finding_becomes_one_line_with_title_and_location(self):
        body = (f"🤖 인트로\n\n1. **토큰이 갱신되지 않습니다**\n\n**문제**\n긴 서술...\n\n"
                f"`src/app.ts`:10\n```ts\ncode\n```\n\n**제안**\n이렇게\n\n{MARKER}\n\n"
                f"2. **가드가 빠졌습니다**\n\n**문제**\n또 긴 서술\n\n{MARKER2}")
        out = ghclient.compact_findings("bot", body)
        self.assertEqual(out.splitlines()[0], "이미 올라간 지적:")
        self.assertIn("- [bot] 토큰이 갱신되지 않습니다 — src/app.ts:10 (rule: stale-token)", out)
        self.assertIn("- [bot] 가드가 빠졌습니다 — src/b.ts:20 (rule: missing-guard)", out)
        self.assertNotIn("긴 서술", out)
        self.assertLess(len(out), len(body))

    def test_section_labels_are_not_mistaken_for_the_title(self):
        """제목이 없으면 **문제** 를 제목으로 집어오면 안 된다 — rule 로 떨어뜨린다."""
        out = ghclient.compact_findings("bot", f"**문제**\n서술\n\n{MARKER}")
        self.assertIn("- [bot] stale-token — src/app.ts:10 (rule: stale-token)", out)

    def test_comment_without_findings_yields_nothing(self):
        self.assertEqual(ghclient.compact_findings("bot", "그냥 댓글"), "")


class FitConversationTest(unittest.TestCase):
    def test_author_parts_survive_and_bot_parts_go_first(self):
        parts = [(True, "봇" * 50), (False, "작성자 해명"), (True, "봇2" * 50)]
        out = ghclient._fit_conversation(parts, 40)
        self.assertIn("작성자 해명", out)
        self.assertNotIn("봇봇", out)
        self.assertIn("오래된 봇 댓글 생략", out)

    def test_oversized_noise_is_dropped_before_cheap_parts(self):
        """CI 로그 한 덩이(60k)가 지적 목록보다 먼저 나가야 한다."""
        noise = (True, "로그" * ghclient.MAX_DROPPABLE_PART)
        cheap = [(True, f"이미 올라간 지적:\n- [bot] 지적{i}") for i in range(5)]
        out = ghclient._fit_conversation([noise] + cheap, 10000)
        self.assertNotIn("로그로그", out)
        for i in range(5):
            self.assertIn(f"지적{i}", out)

    def test_author_only_content_over_budget_keeps_the_newest(self):
        out = ghclient._fit_conversation([(False, "오래된 글" * 100), (False, "최신 글")], 100)
        self.assertTrue(out.endswith("최신 글"))
        self.assertIn("이전 대화 생략", out)

    def test_empty_conversation_is_explicit(self):
        self.assertEqual(ghclient._fit_conversation([], 100), "(이전 대화 없음)")


class PrConversationTest(unittest.TestCase):
    def setUp(self):
        self.saved = ghclient._run
        self.addCleanup(setattr, ghclient, "_run", self.saved)

    def _wire(self, comments, inline=()):
        view = {"author": {"login": "author"},
                "body": "## 요약\n설계\n\n### 보류 항목 — 이 PR 범위 밖\n| 지적 | 사유 |",
                "comments": comments}

        class Proc:
            def __init__(self, out):
                self.returncode, self.stdout = 0, out

        def fake_run(args, check=True):
            if args[0] == "pr":
                return Proc(json.dumps(view))
            return Proc("\n".join(json.dumps(x) for x in inline))

        ghclient._run = fake_run

    def test_body_and_author_reply_survive_while_bot_noise_is_compacted(self):
        self._wire([
            {"author": {"login": "ci"}, "body": "실패 로그\n" + "x" * 60000},
            {"author": {"login": "bot"},
             "body": f"1. **토큰 문제**\n\n**문제**\n{'서술' * 500}\n\n{MARKER}"},
            {"author": {"login": "author"}, "body": "이 지적은 보류입니다 — PO 결정 대기"},
        ])
        out = ghclient.pr_conversation("owner/repo", 1, 4000)
        self.assertIn("보류 항목 — 이 PR 범위 밖", out)          # PR 본문
        self.assertIn("이 지적은 보류입니다", out)                 # 작성자 회신
        self.assertIn("(rule: stale-token)", out)                 # 지적은 한 줄로
        self.assertNotIn("서술서술", out)                          # 원문은 아님
        self.assertNotIn("xxxxx", out)                             # CI 로그는 버림

    def test_inline_bot_comments_are_compacted_too(self):
        self._wire([], inline=[
            {"login": "bot", "path": "src/b.ts", "line": 20,
             "body": f"**가드가 빠졌습니다**\n\n**문제**\n{'서술' * 500}\n\n{MARKER2}"},
        ])
        out = ghclient.pr_conversation("owner/repo", 1, 4000)
        self.assertIn("- [bot] 가드가 빠졌습니다 — src/b.ts:20 (rule: missing-guard)", out)
        self.assertNotIn("서술서술", out)


class AuthorReplyCapTest(unittest.TestCase):
    def test_only_the_newest_replies_are_sent_to_closure(self):
        fp = "owner/repo#1:src/app.ts:10:stale-token"
        comments = [{"id": "0", "author": "bot", "author_id": "9", "created_at": "00",
                     "body": f"<!-- hermes:fp={fp} -->"}]
        comments += [{"id": str(i), "author": "author", "author_id": "42",
                      "created_at": f"{i:02d}", "body": f"회신 {i}"} for i in range(1, 15)]
        body = {"id": "pr-body", "author": "author", "created_at": "00", "body": "본문 보류 표"}
        replies = ghclient.finding_author_replies(comments, fp, "42", "bot", body)
        self.assertEqual(len(replies), ghclient.MAX_AUTHOR_REPLIES + 1)
        self.assertEqual(replies[0]["id"], "pr-body")  # 본문은 상한과 무관하게 항상
        self.assertEqual([r["id"] for r in replies[1:]], [str(i) for i in range(5, 15)])


if __name__ == "__main__":
    unittest.main()
