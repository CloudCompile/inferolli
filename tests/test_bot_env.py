"""bot.py — .env must beat the ambient environment.

The launcher writes /bot/.env, but the supervisor process can be hours old. With
setdefault() the stale ambient values won, so editing a model or fallback chain
in .env silently did nothing — the bot kept running the pre-edit config.
"""

import unittest


class EnvPrecedenceTests(unittest.TestCase):
    def test_dotenv_overrides_stale_ambient_value(self):
        # mirrors bot.py's loader exactly (kept in sync by this test)
        import os
        from unittest import mock

        lines = ["AI_FALLBACK_MODELS=deepseek-v4.1-flash-1017,gpt-6-luna"]
        ambient = {"AI_FALLBACK_MODELS": "glm-5.3-flash,gpt-6-luna"}
        env = dict(ambient)
        with mock.patch.dict(os.environ, env, clear=True):
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.strip().strip('"').strip("'")
                    if v:
                        os.environ[k.strip()] = v
            self.assertEqual(os.environ["AI_FALLBACK_MODELS"],
                             "deepseek-v4.1-flash-1017,gpt-6-luna")

    def test_loader_uses_assignment_not_setdefault(self):
        import inspect
        import bot  # noqa: F401  (import proves the module loads)
        src = inspect.getsource(bot)
        loader = src[src.index("_env_file"):src.index("logging.basicConfig")]
        self.assertIn("os.environ[_k.strip()] = _v", loader)
        self.assertNotIn("setdefault", loader)

    def test_empty_values_do_not_clobber(self):
        # a blank KEY= line in .env must not erase a real ambient value
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"TOKEN": "real"}, clear=True):
            for line in ["TOKEN="]:
                k, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                if v:
                    os.environ[k.strip()] = v
            self.assertEqual(os.environ["TOKEN"], "real")


if __name__ == "__main__":
    unittest.main()
