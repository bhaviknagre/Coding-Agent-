import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import key, provider, screen, services
from agent.verify import Stop


class Services(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / "data")})
        p.start()
        self.addCleanup(p.stop)
        self.keys = {}
        for name, fake in (("load", lambda s, keychain="": self.keys.get(s, "")),
                           ("ask", lambda s: (_ for _ in ()).throw(EOFError()))):
            m = mock.patch(f"agent.key.{name}", fake)
            m.start()
            self.addCleanup(m.stop)

    def test_each_service_uses_its_own_key_address_and_limit(self):
        self.keys = {"groq": "g" * 30, "gemini": "m" * 30}
        model, secrets = screen.choose_model("groq")
        self.assertEqual((model.key, model.name, secrets), ("g" * 30, "groq:llama-3.3-70b-versatile", ("g" * 30,)))
        self.assertTrue(model.address.startswith("https://api.groq.com/"))
        self.assertEqual(model.limiter.gap, 2.0)  # 30 a minute
        self.assertEqual(model.busy, [])
        gemini, _ = screen.choose_model("gemini")
        self.assertEqual((gemini.key, gemini.model), ("m" * 30, "gemini-3.8-flash"))
        self.assertTrue(gemini.busy)
        chosen, _ = screen.choose_model("gemini:gemini-3.5-flash-lite")
        self.assertEqual((chosen.model, chosen.busy), ("gemini-3.5-flash-lite", []))  # a chosen model stays

    def test_no_key_says_how_to_store_it(self):
        with self.assertRaisesRegex(Stop, "agent key set openrouter"):
            screen.choose_model("openrouter")
        with self.assertRaisesRegex(Stop, "agent key list"):
            screen.choose_model("nosuch")

    def test_the_notice_names_who_receives_the_code(self):
        self.keys = {"groq": "g" * 30, "gemini": "m" * 30}
        for name, words in (("groq", ["sent to Groq.", "not tested yet"]),
                            ("gemini", ["sent to Google.", "On the free tier Google may use it"])):
            text = screen.provider_notice(screen.choose_model(name)[0])
            for w in words:
                self.assertIn(w, text)
        self.assertNotIn("not tested", screen.provider_notice(screen.choose_model("gemini")[0]))

    def test_an_added_service_must_use_https_and_saves_no_key(self):
        for url in ("http://x.example/v1", "https://u:p" + chr(64) + "x.example/v1", "https://x.example/v1?k=1", "ftp://x"):
            with self.assertRaises(ValueError, msg=url):
                services.add("mine", url, "m1", "Me")
        for bad in ("Mine", "x", "gemini", "script"):
            with self.assertRaises(ValueError, msg=bad):
                services.add(bad, "https://x.example/v1/chat/completions", "m1", "Me")
        services.add("mine", "https://x.example/v1/chat/completions", "m1", "My company", per_minute=12)
        saved = (services._saved()).read_text()
        self.assertNotIn("key", saved.lower())
        self.keys = {"mine": "k" * 30}
        model, _ = screen.choose_model("mine")
        self.assertEqual((model.address, model.model, model.limiter.gap), (
            "https://x.example/v1/chat/completions", "m1", 5.0))
        self.assertIn("sent to My company.", screen.provider_notice(model))

    def test_a_changed_file_cannot_send_the_key_without_https(self):
        services._saved().parent.mkdir(parents=True, exist_ok=True)
        services._saved().write_text('{"evil": {"address": "http://x.example/", "model": "m", "company": "c"}}')
        self.keys = {"evil": "k" * 30}
        with self.assertRaisesRegex(Stop, "https"):
            screen.choose_model("evil")

    def test_key_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("agent.key.exists", lambda s, keychain="": s == "groq"):
            self.assertEqual(key.main(["list"]), 0)
            self.assertEqual(key.main(["set", "nosuch"]), 2)
            self.assertEqual(key.main(["add", "mine", "--url", "https://x.example/v1", "--model", "m",
                                       "--company", "Me"]), 0)
            self.assertEqual(key.main(["status", "mine"]), 0)
        text = out.getvalue()
        self.assertRegex(text, r"groq +key stored")
        self.assertIn("gemini       no key", text)
        self.assertIn("usage: agent key set|status|remove SERVICE", text)
        self.assertIn("Next: agent key set mine", text)

    def test_errors_name_the_service_in_use(self):
        m = provider.OpenAIFormat("k" * 30, "x", "https://api.groq.com/openai/v1/chat/completions", "groq")
        self.assertEqual(provider.NEXT[503].format(service=m.service),
                         ". Next: try again in a few minutes, or another model: --model groq:MODEL")
