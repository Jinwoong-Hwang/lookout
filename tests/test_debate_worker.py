import json
import sqlite3
import unittest

from src import (db, debate_worker, engines, ghclient, impl_worker, keys, notify,
                 prompt_tpl, worktree)

REPO = "acme/product-hub"
TARGET = "acme/web"


class DebateWorkerTest(unittest.TestCase):
    """브로커가 턴을 소유한다. 한 번 호출에 한 라운드만 돌고, 종료는 3중 조건으로만
    일어나며, 끝나면 사람 승인 게이트에 선다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.key = keys.issue_key(REPO, 1816)
        self.card_id = db.upsert_card(
            self.c, self.key, "issue", REPO, 1816, status="spec",
            payload={"display": "PH-1816", "title": "임대인 온보딩 로그인 우회",
                     "url": "u", "labels": ["Service::ZB"], "mode": "debate",
                     "instruction": "가드는 라우트에서 풀어라"})
        self.saved = {"view": ghclient.issue_view, "mk": worktree.make_impl_worktree,
                      "run": engines.run, "ready": engines.is_ready,
                      "render": prompt_tpl.render, "notify": notify.send,
                      "map": impl_worker.CFG.get("impl_target_map"),
                      "roles": debate_worker.CFG.get("debate_roles")}
        impl_worker.CFG["impl_target_map"] = {"ZB": TARGET}
        debate_worker.CFG["debate_roles"] = {"proposer": "claude", "critic": "codex"}
        ghclient.issue_view = lambda *_a, **_k: {"title": "t", "body": "본문", "url": "u"}
        worktree.make_impl_worktree = lambda *_a, **_k: "/wt"
        engines.is_ready = lambda _e: True
        self.prompts = []
        prompt_tpl.render = lambda name, **kw: self.prompts.append((name, kw)) or "PROMPT"
        self.notes = []
        notify.send = lambda *a, **k: self.notes.append(a) or True
        self.turns = []

    def tearDown(self):
        ghclient.issue_view = self.saved["view"]
        worktree.make_impl_worktree = self.saved["mk"]
        engines.run = self.saved["run"]
        engines.is_ready = self.saved["ready"]
        prompt_tpl.render = self.saved["render"]
        notify.send = self.saved["notify"]
        for key, box in (("impl_target_map", impl_worker.CFG), ("debate_roles", debate_worker.CFG)):
            if self.saved["map" if key == "impl_target_map" else "roles"] is None:
                box.pop(key, None)
            else:
                box[key] = self.saved["map" if key == "impl_target_map" else "roles"]
        self.c.close()

    def _reply(self, *replies):
        seq = list(replies)
        def fake(prompt, engine="claude", **kw):
            self.turns.append(engine)
            return json.dumps(seq.pop(0), ensure_ascii=False) if seq else json.dumps(
                {"claim": "기본", "verdict": "CONTINUE"})
        engines.run = fake

    def _card(self):
        return db.get_card(self.c, self.key)

    def _payload(self):
        return json.loads(self._card()["payload"])

    def _events(self):
        return [r["type"] for r in self.c.execute("SELECT type FROM events").fetchall()]

    def _round(self):
        debate_worker.process(self.c, self._card())

    def test_one_call_runs_one_round_and_alternates_roles(self):
        self._reply({"claim": "안 v1", "verdict": "CONTINUE"},
                    {"claim": "반박 1", "verdict": "CONTINUE"})
        self._round()
        self.assertEqual(self._card()["status"], "spec")       # 아직 토론 중
        self.assertEqual(len(self._payload()["debate"]), 1)
        self.assertEqual(self._payload()["debate"][0]["role"], "proposer")
        self._round()
        turns = self._payload()["debate"]
        self.assertEqual([t["role"] for t in turns], ["proposer", "critic"])
        self.assertEqual(self.turns, ["claude", "codex"])      # 두 엔진이 갈린다

    def test_both_agree_stops_and_opens_the_human_gate(self):
        self._reply({"claim": "안", "proposal": "이렇게 한다", "verdict": "AGREE"},
                    {"claim": "못 이겼다", "verdict": "AGREE"})
        self._round(); self._round()
        card = self._card()
        self.assertEqual(card["status"], "spec_blocked")
        self.assertEqual(card["blocked"], 1)                   # 사람이 눌러야 진행
        ag = self._payload()["agreement"]
        self.assertEqual(ag["design"], "이렇게 한다")
        self.assertEqual(ag["rounds"], 2)
        self.assertIn("debate_finished", self._events())
        self.assertTrue(self.notes, "합의되면 사람에게 알려야 한다")

    def test_repeated_claim_stops_the_polite_loop(self):
        same = {"claim": "같은 주장", "verdict": "CONTINUE"}
        self._reply(same, {"claim": "반박", "verdict": "CONTINUE"}, dict(same))
        self._round(); self._round(); self._round()
        self.assertEqual(self._card()["status"], "spec_blocked")
        end = [r for r in self.c.execute(
            "SELECT detail FROM events WHERE type='debate_finished'").fetchall()]
        self.assertIn("새 정보 없음", end[0]["detail"])

    def test_blocked_verdict_ends_and_is_marked(self):
        self._reply({"claim": "안", "verdict": "CONTINUE"},
                    {"claim": "받을 수 없다", "verdict": "BLOCKED"})
        self._round(); self._round()
        self.assertEqual(self._card()["status"], "spec_blocked")
        self.assertTrue(self._payload()["agreement"]["blocked"])

    def test_round_cap_stops_even_without_agreement(self):
        self._reply(*[{"claim": f"주장{i}", "verdict": "CONTINUE"}
                      for i in range(debate_worker.MAX_ROUNDS)])
        for _ in range(debate_worker.MAX_ROUNDS):
            self._round()
        self.assertEqual(self._card()["status"], "spec_blocked")
        self.assertEqual(self._payload()["agreement"]["rounds"], debate_worker.MAX_ROUNDS)

    def test_unresolved_questions_are_carried_to_the_gate(self):
        self._reply({"claim": "안", "proposal": "P", "verdict": "AGREE",
                     "open_questions": ["세션에서 판정? 서버에서?"]},
                    {"claim": "ok", "verdict": "AGREE", "open_questions": ["CI 시점"]})
        self._round(); self._round()
        unresolved = self._payload()["agreement"]["unresolved"]
        self.assertIn("세션에서 판정? 서버에서?", unresolved)
        self.assertIn("CI 시점", unresolved)

    def test_missing_engine_fails_instead_of_self_debating(self):
        engines.is_ready = lambda e: e == "claude"
        self._reply({"claim": "안", "verdict": "CONTINUE"})
        self._round()                     # r1 proposer=claude 는 통과
        self._round()                     # r2 critic=codex 없음
        self.assertEqual(self._card()["status"], "failed")
        self.assertIn("debate_engine_missing", self._events())

    def test_target_repo_comes_from_the_label(self):
        self._reply({"claim": "안", "verdict": "CONTINUE"})
        self._round()
        self.assertEqual(self._payload()["target_repo"], TARGET)

    def test_engines_stay_read_only(self):
        import inspect
        src = inspect.getsource(debate_worker.process)
        self.assertIn("engines.run(", src)
        self.assertNotIn("run_impl", src)
        # 설치는 돌리지 않는다 — 토론은 코드를 읽기만 한다
        self.assertIn("setup=False", inspect.getsource(debate_worker._context))

    def test_prompt_carries_the_transcript_from_round_two(self):
        self._reply({"claim": "안 v1", "proposal": "P1", "verdict": "CONTINUE"},
                    {"claim": "반박", "verdict": "CONTINUE"})
        self._round(); self._round()
        name, kw = self.prompts[-1]
        self.assertEqual(name, "debate.critic.md")
        self.assertIn("안 v1", kw["TRANSCRIPT"])
        self.assertEqual(kw["ROUND"], 2)


if __name__ == "__main__":
    unittest.main()


class OperatorSteerTest(DebateWorkerTest):
    """합의에 사람이 피드백을 줄 수 있어야 한다 — 승인하며 수정 지시를 얹거나,
    방향을 주고 토론을 재개하거나."""

    def _steer(self, text="가드는 미들웨어가 아니라 라우트에서 풀어라"):
        meta = self._payload()
        db.merge_payload(self.c, self.card_id, {
            "debate": (meta.get("debate") or []) + [{"role": "operator", "claim": text}],
            "debate_bonus": int(meta.get("debate_bonus") or 0) + 2, "agreement": {}})
        db.set_status(self.c, self.card_id, "spec", blocked=0)

    def test_operator_turn_does_not_shift_the_role_rotation(self):
        self._reply({"claim": "안 v1", "verdict": "CONTINUE"},
                    {"claim": "반박", "verdict": "CONTINUE"})
        self._round()          # r1 proposer
        self._steer()          # 사람 개입
        self._round()          # 개입 뒤에도 다음은 critic 이어야 한다
        roles = [t["role"] for t in self._payload()["debate"]]
        self.assertEqual(roles, ["proposer", "operator", "critic"])
        self.assertEqual(self.turns, ["claude", "codex"])

    def test_steer_appears_in_the_next_prompt_as_authoritative(self):
        self._reply({"claim": "안 v1", "verdict": "CONTINUE"},
                    {"claim": "반박", "verdict": "CONTINUE"})
        self._round()
        self._steer("라우트에서 풀어라")
        self._round()
        _name, kw = self.prompts[-1]
        self.assertIn("운영자 개입", kw["TRANSCRIPT"])
        self.assertIn("라우트에서 풀어라", kw["TRANSCRIPT"])

    def test_same_claim_before_a_steer_does_not_end_the_debate(self):
        """사람이 방향을 틀면 새 국면이다. 개입 전 주장과 같아졌다고 끝내면
        피드백이 무시된 채 종료된다."""
        same = {"claim": "같은 주장", "verdict": "CONTINUE"}
        self._reply(same, {"claim": "반박", "verdict": "CONTINUE"}, dict(same))
        self._round(); self._round()
        self._steer()
        self._round()
        self.assertEqual(self._card()["status"], "spec")   # 계속 돈다

    def test_bonus_rounds_let_a_capped_debate_resume(self):
        self._reply(*[{"claim": f"c{i}", "verdict": "CONTINUE"}
                      for i in range(debate_worker.MAX_ROUNDS + 2)])
        for _ in range(debate_worker.MAX_ROUNDS):
            self._round()
        self.assertEqual(self._card()["status"], "spec_blocked")   # 상한
        self._steer()                                              # +2 라운드
        self._round()
        self.assertEqual(self._card()["status"], "spec")           # 다시 돈다

    def test_agreement_counts_engine_rounds_only(self):
        self._reply({"claim": "안", "proposal": "P", "verdict": "AGREE"},
                    {"claim": "ok", "verdict": "AGREE"})
        self._round()
        self._steer()
        self._round()
        ag = self._payload()["agreement"]
        self.assertEqual(ag["rounds"], 2)     # operator 턴은 라운드가 아니다
        self.assertEqual(ag["steers"], 1)


class TopicDebateTest(unittest.TestCase):
    """이슈 없이 주제만으로도 토론이 돌아야 한다. 구현으로는 가지 않는다."""

    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.saved = {"parent": worktree.impl_parent, "mk": worktree.make_impl_worktree}
        self.made = []
        worktree.make_impl_worktree = lambda *a, **k: self.made.append(a) or "/wt"
        worktree.impl_parent = lambda r: "/checkouts/" + r.split("/")[-1]

    def tearDown(self):
        worktree.impl_parent = self.saved["parent"]
        worktree.make_impl_worktree = self.saved["mk"]
        self.c.close()

    def _card(self, **meta):
        base = {"display": "TOPIC-1", "title": "t", "topic": "주제",
                "mode": "debate_only"}
        base.update(meta)
        key = keys.topic_key(1, 1000.0)
        db.upsert_card(self.c, key, "issue", "-", 0, status="spec", payload=base)
        return db.get_card(self.c, key)

    def test_topic_with_a_repo_reads_the_checkout_without_making_a_branch(self):
        card = self._card(target_repo="acme/web")
        repo, cwd = debate_worker._context(self.c, card, json.loads(card["payload"]))
        self.assertEqual((repo, cwd), ("acme/web", "/checkouts/web"))
        self.assertEqual(self.made, [], "주제 토론은 구현 브랜치를 만들지 않는다")

    def test_topic_without_a_repo_falls_back_to_lookout_itself(self):
        card = self._card()
        repo, cwd = debate_worker._context(self.c, card, json.loads(card["payload"]))
        self.assertEqual(repo, "")
        self.assertEqual(cwd, debate_worker.config.HERMES_HOME)

    def test_unconfigured_repo_does_not_stop_the_debate(self):
        worktree.impl_parent = lambda _r: (_ for _ in ()).throw(
            worktree.ImplRepoUnknown("설정 없음"))
        card = self._card(target_repo="acme/unknown")
        repo, cwd = debate_worker._context(self.c, card, json.loads(card["payload"]))
        self.assertEqual(repo, "acme/unknown")
        self.assertEqual(cwd, debate_worker.config.HERMES_HOME)

    def test_issue_card_still_gets_a_branch_and_worktree(self):
        card = self._card(mode="debate", target_repo="acme/web", display="PH-9")
        repo, cwd = debate_worker._context(self.c, card, json.loads(card["payload"]))
        self.assertEqual((repo, cwd), ("acme/web", "/wt"))
        self.assertTrue(self.made, "이슈 토론은 구현이 이어받을 워크트리를 만든다")
