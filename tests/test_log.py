import os
import random
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent.log import End, Log
from tests.test_mask import MARK

WRITER = """
import sys
from pathlib import Path
from agent.log import Log
log = Log(Path(sys.argv[1]))
while True:
    print(log.add("t", "step", text="x" * 400).seq, flush=True)
"""


class LogTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "private" / "events.db"

    def open(self, **k):
        log = Log(self.path, **k)
        self.addCleanup(log.close)
        return log

    def test_twenty_kills_lose_nothing(self):
        total = 0
        for i in range(20):
            path = self.path.with_name(f"{i}.db")
            p = subprocess.Popen([sys.executable, "-c", WRITER, str(path)], stdout=subprocess.PIPE, text=True)
            first = p.stdout.readline()
            time.sleep(random.uniform(0.01, 0.15))  # noqa: S311
            p.send_signal(signal.SIGKILL)
            confirmed = int((first + p.stdout.read()).rsplit("\n", 1)[0].split("\n")[-1])
            p.stdout.close()
            p.wait()
            log = Log(path)
            self.assertGreaterEqual(log.end().seq, confirmed)
            self.assertEqual(log.verify(), "")
            log.close()
            total += confirmed
        self.assertGreater(total, 20)

    def test_every_changed_field_is_found(self):
        log = self.open()
        for i in range(5):
            log.add("t", "k", n=i)
        for field in ("at", "task", "kind", "body", "prev", "hash"):
            log.db.execute("begin")
            log.db.execute(f"update event set {field}='1' where seq=3")  # noqa: S608
            self.assertIn("event 3", log.verify(), field)
            log.db.execute("rollback")

    def test_removed_events(self):
        log = self.open()
        for i in range(5):
            end = log.add("t", "k", n=i)
        log.db.execute("begin")
        log.db.execute("delete from event where seq=2")
        self.assertIn("event 2", log.verify())
        log.db.execute("rollback")
        log.db.execute("delete from event where seq=5")
        self.assertEqual(log.verify(), "")  # the chain alone cannot see this
        self.assertIn("receipt", log.verify(end))

    def test_an_older_receipt_stays_valid(self):
        log = self.open()
        self.assertEqual(log.verify(End(0, "0" * 64)), "")
        old = log.add("t", "k")
        log.add("t", "k")
        self.assertEqual(log.verify(old), "")

    def test_secrets_are_masked_before_storage(self):
        log = self.open(secrets=(MARK, 'qu"o\nte'))
        log.add("t " + MARK, "k", text=f"a {MARK}", deep=[{MARK: (MARK,)}], odd='qu"o\nte')
        log.close()
        data = b"".join(p.read_bytes() for p in self.path.parent.iterdir())
        self.assertNotIn(MARK.encode(), data)
        self.assertNotIn(b"o\\nte", data)

    def test_only_the_owner_can_read(self):
        old = os.umask(0)
        try:
            self.open().add("t", "k")
        finally:
            os.umask(old)
        for p in [self.path.parent, *self.path.parent.iterdir()]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0, p.name)

    def test_two_writers_make_one_chain(self):
        a, b = self.open(), self.open()
        for _ in range(20):
            a.add("a", "k")
            b.add("b", "k")
        self.assertEqual((a.verify(), a.end().seq, len(a.events("b"))), ("", 40, 20))

    def test_newer_log_is_refused(self):
        self.open().db.execute("pragma user_version=99")
        with self.assertRaisesRegex(ValueError, "update the program"):
            Log(self.path)

    def test_unstorable_value_adds_nothing(self):
        log = self.open()
        with self.assertRaises(TypeError):
            log.add("t", "k", bad=object())
        self.assertEqual(log.end().seq, 0)
        log.add("t", "k")
