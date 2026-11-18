"""ai.py — provider routing, MoM jobs, JSON envelopes, fallback chain."""

import asyncio
import os
import unittest
from unittest import mock

import ai


class ProviderRoutingTests(unittest.TestCase):
    def test_slash_ids_route_to_gateway_when_configured(self):
        with mock.patch.multiple(ai, GW_BASE="https://gw.example", GW_KEY="k"):
            self.assertEqual(ai._provider_for("stealth/glyph-cluster")[0], "gateway")
            self.assertEqual(ai._provider_for("gpt-6-luna")[0], "port")

    def test_no_gateway_config_means_port(self):
        with mock.patch.multiple(ai, GW_BASE="", GW_KEY=""):
            self.assertEqual(ai._provider_for("stealth/glyph-cluster")[0], "port")

    def test_job_model_env_override(self):
        with mock.patch.dict(os.environ, {"AI_JOB_DREAM": "gemini-4-hydrogen"}):
            self.assertEqual(ai._job_model("dream"), "gemini-4-hydrogen")
            self.assertIsNone(ai._job_model("nonexistent_job"))

    def test_job_model_normalizes_dashes(self):
        with mock.patch.dict(os.environ, {"AI_JOB_QUICK_THING": "m"}):
            self.assertEqual(ai._job_model("quick-thing"), "m")


class ChatForJsonTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_plain_json(self):
        async def fake_chat(*a, **k):
            return '{"reply": "hi"}'
        with mock.patch.object(ai, "chat", fake_chat):
            self.assertEqual(self.run_async(ai.chat_for("reply", [])), '{"reply": "hi"}')
            self.assertEqual(self.run_async(ai.chat_for("reply", [], json_out=True)),
                             {"reply": "hi"})

    def test_fenced_json(self):
        async def fake_chat(*a, **k):
            return '```json\n{"actions": []}\n```'
        with mock.patch.object(ai, "chat", fake_chat):
            self.assertEqual(self.run_async(ai.chat_for("reply", [], json_out=True)),
                             {"actions": []})

    def test_invalid_json_raises(self):
        async def fake_chat(*a, **k):
            return "lol no json"
        with mock.patch.object(ai, "chat", fake_chat):
            with self.assertRaises(ai.AIError):
                self.run_async(ai.chat_for("reply", [], json_out=True))

    def test_non_object_json_raises(self):
        async def fake_chat(*a, **k):
            return "[1, 2, 3]"
        with mock.patch.object(ai, "chat", fake_chat):
            with self.assertRaises(ai.AIError):
                self.run_async(ai.chat_for("reply", [], json_out=True))


class ChatChainTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._saved = {k: getattr(ai, k) for k in
                       ("MODEL", "FALLBACK_MODELS", "GW_MODEL", "GW_KEY", "GW_BASE")}
        ai.MODEL = "primary"
        ai.FALLBACK_MODELS = ["fallback"]
        ai.GW_MODEL = ""
        ai.GW_KEY = ""
        ai.GW_BASE = ""

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(ai, k, v)

    def run_async(self, coro):
        return asyncio.run(coro)

    def _ok(self, model, content="fine"):
        return {"choices": [{"message": {"content": content}}]}

    def test_falls_to_next_model_on_upstream_5xx(self):
        async def fake_post(path, payload, *, timeout, retries=2, provider=None):
            self.calls.append(payload["model"])
            if payload["model"] == "primary":
                raise ai.AIError("AI API returned 503: down")
            return self._ok(payload["model"], "from fallback")

        with mock.patch.object(ai, "_post", fake_post), \
             mock.patch.object(ai.asyncio, "sleep", self._nosleep):
            out = self.run_async(ai.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, "from fallback")
        self.assertEqual(self.calls, ["primary", "fallback"])

    async def _nosleep(self, *a, **k):
        return None

    def test_empty_content_with_reasoning_retries_with_bigger_budget(self):
        payloads = []

        async def fake_post(path, payload, *, timeout, retries=2, provider=None):
            payloads.append(dict(payload))
            if len(payloads) == 1:
                return {"choices": [{"message": {"content": "",
                                                 "reasoning": "thinking hard"}}]}
            return self._ok(payload["model"], "second try")

        ai.FALLBACK_MODELS = []          # single-model chain: the retry is the budget bump
        with mock.patch.object(ai, "_post", fake_post), \
             mock.patch.object(ai.asyncio, "sleep", self._nosleep):
            out = self.run_async(ai.chat([], max_tokens=50))
        self.assertEqual(out, "second try")
        self.assertGreater(payloads[1]["max_tokens"], payloads[0]["max_tokens"])

    def test_gateway_payment_error_falls_through_to_port(self):
        seen = []

        async def fake_post(path, payload, *, timeout, retries=2, provider=None):
            seen.append(payload["model"])
            if payload["model"] == "gw/model":
                raise ai.AIError("gateway needs payment setup: 402")
            return self._ok(payload["model"], "port answer")

        ai.GW_MODEL = "gw/model"
        ai.GW_KEY = "k"
        ai.GW_BASE = "https://gw"
        with mock.patch.object(ai, "_post", fake_post), \
             mock.patch.object(ai.asyncio, "sleep", self._nosleep):
            out = self.run_async(ai.chat([]))
        self.assertEqual(out, "port answer")
        self.assertEqual(seen[0], "gw/model")
        self.assertIn("primary", seen)

    def test_hard_failure_raises(self):
        async def fake_post(*a, **k):
            raise ai.AIError("AI API returned 400: bad")

        with mock.patch.object(ai, "_post", fake_post):
            with self.assertRaises(ai.AIError):
                self.run_async(ai.chat([]))

    def test_no_api_key_is_an_error(self):
        with mock.patch.object(ai, "API_KEY", ""):
            with self.assertRaises(ai.AIError):
                self.run_async(ai._post("/x", {"model": "m"}, timeout=1))


class PortLockedTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_embeddings_always_use_port(self):
        seen = {}

        async def fake_post(path, payload, *, timeout, retries=2, provider=None):
            seen["provider"] = provider
            return {"data": [{"embedding": [0.1, 0.2]}]}

        with mock.patch.object(ai, "_post", fake_post):
            vecs = self.run_async(ai.embed(["hello"]))
        self.assertEqual(seen["provider"], "port")
        self.assertEqual(vecs, [[0.1, 0.2]])

    def test_decide_always_uses_port(self):
        seen = {}

        async def fake_post(path, payload, *, timeout, retries=2, provider=None):
            seen["provider"] = provider
            return {"answers": {"tier": {"choice": "⚓ Sailor", "confidence": 0.9}}}

        with mock.patch.object(ai, "_post", fake_post):
            out = self.run_async(ai.decide("state", {"tier": {}}))
        self.assertEqual(seen["provider"], "port")
        self.assertEqual(out["tier"]["choice"], "⚓ Sailor")

    def test_decide_bad_shape_raises(self):
        async def fake_post(*a, **k):
            return {"nope": True}

        with mock.patch.object(ai, "_post", fake_post):
            with self.assertRaises(ai.AIError):
                self.run_async(ai.decide("s", {}))


if __name__ == "__main__":
    unittest.main()
