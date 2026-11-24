import unittest

from agent.mask import MASK, StreamMask, mask
from tests.test_scan import FAKES

MARK = "FAKE-KEY-7c1e93ab52d04f"


def stream(text, size, secrets=()):
    m = StreamMask(secrets)
    return "".join([*(m.feed(text[i : i + size]) for i in range(0, len(text), size)), m.end()])


class Mask(unittest.TestCase):
    def test_exact_secret_at_every_cut(self):
        text = "x" * 9000 + MARK + "y" * 9000 + MARK
        for size in [*range(1, 41), 4095, 4096, 8193]:
            out = stream(text, size, (MARK,))
            self.assertNotIn(MARK, out)
            self.assertEqual(out, "x" * 9000 + MASK + "y" * 9000 + MASK)

    def test_pattern_secret_at_every_cut_near_the_release_point(self):
        secret = FAKES["google-key"]
        for pad in range(8192 - 60, 8192 + 60):
            out = stream(" " * pad + secret + " " * 9000, 1000)
            self.assertNotIn(secret[:20], out, pad)

    def test_tail_of_a_long_key_never_leaks(self):
        secret = "s" + "k-" + "Ab1" * 1200
        for pad in (0, 100, 4000, 4096, 5000, 8000, 8190):
            for size in (1, 64, 1000):
                out = stream(" " * pad + secret + " " * 9000, size)
                self.assertEqual(out.strip(), MASK, (pad, size))

    def test_nothing_is_lost(self):
        text = "line\n" * 5000
        self.assertEqual(stream(text, 7), text)

    def test_empty_secret_is_ignored(self):
        self.assertEqual(mask("abc", ("",)), "abc")

    def test_longer_secret_first(self):
        self.assertEqual(mask("abcdef", ("abc", "abcdef")), MASK)

    def test_known_limit_changed_forms_are_not_found(self):
        self.assertIn(MARK[::-1], mask(MARK[::-1], (MARK,)))
