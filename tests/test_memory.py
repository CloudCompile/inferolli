"""memory.py — STM, recall, tiers, ranks, persona evolution, decay."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {k: getattr(memory, k) for k in (
            "MEM_DIR", "USER_DIR", "PERSONA_FILE", "SERVER_FILE", "STM_FILE",
            "THOUGHTS_FILE", "RANKS_FILE", "STM_EMB_FILE")}
        memory.MEM_DIR = self.root
        memory.USER_DIR = self.root / "users"
        memory.PERSONA_FILE = self.root / "persona.json"
        memory.SERVER_FILE = self.root / "server.json"
        memory.STM_FILE = self.root / "stm.json"
        memory.THOUGHTS_FILE = self.root / "thoughts.json"
        memory.RANKS_FILE = self.root / "ranks.json"
        memory.STM_EMB_FILE = self.root / "stm-emb.json"
        self._embed = memory.ai.embed
        memory.ai.embed = self._no_embed

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(memory, k, v)
        memory.ai.embed = self._embed
        self.tmp.cleanup()

    @staticmethod
    async def _no_embed(texts):
        raise memory.AIError("no network in tests")

    def run_async(self, coro):
        return asyncio.run(coro)

    # ---- STM ----

    def test_remember_and_window(self):
        async def go():
            await memory.remember_stm(1, "a", "#general", "hello world")
            await memory.remember_stm(2, "b", "#general", "second")
            return await memory.stm_window(hours=1)

        recent = self.run_async(go())
        self.assertEqual(len(recent), 2)
        self.assertEqual([e["text"] for e in recent], ["hello world", "second"])

    def test_stm_window_excludes_old_entries(self):
        stm = {"entries": [
            {"id": 1, "user_id": 1, "name": "a", "channel": "c", "text": "old",
             "ts": time.time() - 8 * 86400},
            {"id": 2, "user_id": 1, "name": "a", "channel": "c", "text": "new",
             "ts": time.time()},
        ], "next_id": 3}
        memory.STM_FILE.write_text(json.dumps(stm))
        window = self.run_async(memory.stm_window(hours=24))
        self.assertEqual([e["text"] for e in window], ["new"])

    def test_stm_cap_and_age_trim(self):
        async def go():
            for i in range(12):
                await memory.remember_stm(1, "a", "c", f"m{i}")
            return json.loads(memory.STM_FILE.read_text())

        with mock.patch.object(memory, "STM_MAX", 5):
            stm = self.run_async(go())
        self.assertEqual(len(stm["entries"]), 5)
        self.assertEqual(stm["entries"][-1]["text"], "m11")
        self.assertEqual(stm["next_id"], 13)

    def test_recall_orders_by_keyword_relevance(self):
        async def go():
            await memory.remember_stm(1, "a", "c", "the lighthouse keeper told a story")
            await memory.remember_stm(1, "a", "c", "completely unrelated chatter")
            return await memory.recall_stm(terms=["lighthouse"], limit=2)

        hits = self.run_async(go())
        self.assertIn("lighthouse", hits[0]["text"])

    def test_recall_excludes_channel(self):
        async def go():
            await memory.remember_stm(1, "a", "#one", "lighthouse here")
            await memory.remember_stm(1, "a", "#two", "lighthouse elsewhere")
            return await memory.recall_stm(terms=["lighthouse"], exclude_channel="#one")

        self.assertEqual([h["channel"] for h in self.run_async(go())], ["#two"])

    # ---- tiers ----

    def test_tier_boundaries(self):
        cases = {0: "🪵 Landlubber", 19: "🪵 Landlubber", 20: "🪝 Deckhand",
                 39: "🪝 Deckhand", 40: "⚓ Sailor", 59: "⚓ Sailor",
                 60: "🧭 Navigator", 74: "🧭 Navigator", 75: "⛵ Bosun",
                 89: "⛵ Bosun", 90: "👑 Admiral", 100: "👑 Admiral"}
        for score, tier in cases.items():
            self.assertEqual(memory.tier_for_score(score), tier, score)

    def test_affinity_tiers(self):
        self.assertEqual(memory.affinity_tier(75), "day-one")
        self.assertEqual(memory.affinity_tier(74), "friend")
        self.assertEqual(memory.affinity_tier(50), "friend")
        self.assertEqual(memory.affinity_tier(49), "familiar")
        self.assertEqual(memory.affinity_tier(10), "new face")

    # ---- ranks ----

    def test_rank_defaults_and_roundtrip(self):
        async def go():
            r = await memory.get_rank(7)
            r["score"] = 55
            r["tier"] = "⚓ Sailor"
            await memory.save_rank(7, r)
            return await memory.get_rank(7), await memory.all_ranks()

        got, all_ = self.run_async(go())
        self.assertEqual(got["score"], 55)
        self.assertEqual(got["tier"], "⚓ Sailor")
        self.assertIn("7", all_)

    def test_partial_rank_gains_defaults(self):
        memory.RANKS_FILE.write_text(json.dumps({"7": {"score": 12}}))
        r = self.run_async(memory.get_rank(7))
        self.assertEqual(r["tier"], memory.TIERS[0][0])
        self.assertEqual(r["history"], [])

    # ---- user LTM ----

    def test_apply_user_updates_dedupes_and_caps(self):
        async def go():
            await memory.apply_user_updates(1, "alice", {"facts": [
                {"text": "likes tea", "salience": 3},
                {"text": "LIKES TEA", "salience": 3},     # case-dup: dropped
            ], "affinity_delta": 500})
            mem = await memory.get_user(1)
            for i in range(30):
                await memory.apply_user_updates(1, "alice", {"facts": [
                    {"text": f"fact {i}", "salience": 1}]})
            mem = await memory.get_user(1)
            return mem

        mem = self.run_async(go())
        texts = [f["text"].lower() for f in mem["facts"]]
        self.assertEqual(sum(t == "likes tea" for t in texts), 1)
        self.assertLessEqual(len(mem["facts"]), 20)
        self.assertEqual(mem["affinity"], 100)      # clamped

    def test_affinity_floor(self):
        self.run_async(memory.apply_user_updates(1, "a", {"affinity_delta": -999}))
        mem = self.run_async(memory.get_user(1))
        self.assertEqual(mem["affinity"], 0)

    def test_user_summary_new_vs_known(self):
        self.assertIn("new face", memory.user_summary(memory._default_user()))
        mem = memory._default_user()
        mem["names"] = ["alice"]
        mem["affinity"] = 80
        mem["facts"] = [{"text": "runs a homelab", "salience": 3, "ts": time.time()}]
        brief = memory.user_summary(mem)
        self.assertIn("day-one", brief)
        self.assertIn("runs a homelab", brief)

    # ---- persona ----

    def test_opinion_new_change_and_reaffirm(self):
        p = memory.default_persona()
        self.assertTrue(memory.apply_opinion_update(
            p, {"topic": "yaml", "stance": "tolerable at small scale", "strength": 3}))
        self.assertTrue(memory.apply_opinion_update(
            p, {"topic": "YAML", "stance": "actually fine", "strength": 3}))
        op = next(o for o in p["opinions"] if o["topic"].lower() == "yaml")
        self.assertEqual(op["stance"], "actually fine")
        self.assertEqual(op["history"][-1]["stance"], "tolerable at small scale")
        self.assertFalse(memory.apply_opinion_update(
            p, {"topic": "yaml", "stance": "actually fine"}))
        self.assertFalse(memory.apply_opinion_update(p, {"topic": "", "stance": "x"}))

    # ---- thoughts ----

    def test_thoughts_cap_and_prune(self):
        async def go():
            for i in range(5):
                await memory.save_thought(f"thought {i}", "c")
            store = json.loads(memory.THOUGHTS_FILE.read_text())
            # age everything
            for t in store["thoughts"]:
                t["ts"] = time.time() - 9 * 86400
            memory.THOUGHTS_FILE.write_text(json.dumps(store))
            dropped = await memory.prune_old_thoughts(days=7)
            return store, dropped

        with mock.patch.object(memory, "THOUGHT_CAP", 3):
            store, dropped = self.run_async(go())
        self.assertEqual(len(store["thoughts"]), 3)
        self.assertEqual(dropped, 3)

    def test_decay_drops_old_low_salience(self):
        async def go():
            mem = memory._default_user()
            now = time.time()
            mem["memories"] = [
                {"text": "ancient trivia", "salience": 1, "ts": now - 40 * 86400},
                {"text": "important anchor", "salience": 5, "ts": now - 40 * 86400},
                {"text": "fresh thing", "salience": 1, "ts": now},
            ]
            await memory.save_user(1, mem)
            dropped = await memory.decay_all_users()
            mem = await memory.get_user(1)
            return dropped, [m["text"] for m in mem["memories"]]

        dropped, kept = self.run_async(go())
        self.assertEqual(dropped, 1)
        self.assertNotIn("ancient trivia", kept)
        self.assertIn("important anchor", kept)
        self.assertIn("fresh thing", kept)


if __name__ == "__main__":
    unittest.main()
