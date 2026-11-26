import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import box, dashboard
from agent.log import Log


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "events.db"
        log = Log(self.path)
        log.add("t1", "task.started", contract={"what": "fix add"}, model="fake")
        log.add("t1", "model.asked", number=1)
        log.add("t1", "tool.asked", tool="edit", decision="ASK", rule="r")
        log.add("t1", "task.ended", state="VERIFIED", reason="", commit="c")
        log.add("v1", "verify.started", result="abc123", contract={})
        log.add("v1", "verify.done", verdict="FAILED", steps=[])
        log.close()

    def rows(self, sql):
        db = sqlite3.connect(self.dir / "copy" / "events.db")
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def test_views_show_tasks_and_verify_runs(self):
        self.assertEqual(dashboard.snapshot(self.path, self.dir / "copy"), "intact")
        got = self.rows("select task, command, request, model, verdict, model_calls, tool_calls, asked_you "
                        "from tasks order by task")
        self.assertEqual(got, [("t1", "agent", "fix add", "fake", "VERIFIED", 1, 1, 1),
                               ("v1", "agent verify", "commit abc123", None, "FAILED", 0, 0, 0)])
        self.assertEqual(self.rows("select verdict from verdicts order by at"), [("VERIFIED",), ("FAILED",)])

    def test_the_log_itself_is_not_changed(self):
        before = self.path.read_bytes()
        dashboard.snapshot(self.path, self.dir / "copy")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows("pragma journal_mode"), [("delete",)])  # readable from a read-only mount

    def test_a_changed_log_is_shown(self):
        db = sqlite3.connect(self.path)
        db.execute("update event set body = '{}' where seq = 2")
        db.commit()
        db.close()
        self.assertEqual(dashboard.snapshot(self.path, self.dir / "copy"), "CHANGED at event 2")
        self.assertEqual(self.rows("select state from integrity"), [("CHANGED at event 2",)])

    def test_no_log_yet(self):
        self.assertEqual(dashboard.snapshot(self.dir / "none.db", self.dir / "copy"), "intact")
        self.assertEqual(self.rows("select count(*) from tasks"), [(0,)])

    def test_every_panel_query_runs(self):
        dashboard.snapshot(self.path, self.dir / "copy")
        db = sqlite3.connect(self.dir / "copy" / "events.db")
        for panel in dashboard.board()["panels"]:
            db.execute(panel["targets"][0]["queryText"]).fetchall()  # raises if a query is wrong
        db.close()


class Containers(unittest.TestCase):
    def test_only_this_computer_and_no_rights(self):
        for image, args in ((dashboard.GRAFANA, dashboard.grafana_args(Path("/d"), 3000)),
                            (dashboard.DATASETTE, dashboard.datasette_args(Path("/d"), 8001))):
            args = args[:args.index(image)]  # Docker's options; after the image come the program's own
            ports = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
            self.assertTrue(ports and all(p.startswith("127.0.0.1:") for p in ports), ports)
            for flag in ("--read-only", "--cap-drop", "no-new-privileges", f"{box.LABEL}=dashboard"):
                self.assertIn(flag, args)
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
            self.assertTrue(mounts and all(m.endswith(":ro") for m in mounts), mounts)
            self.assertNotIn("/var/run/docker.sock", " ".join(args))
        grafana = " ".join(dashboard.grafana_args(Path("/d"), 3000))
        for off in ("GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer", "GF_ANALYTICS_REPORTING_ENABLED=false",
                    "GF_AUTH_DISABLE_LOGIN_FORM=true", "GF_PLUGINS_PREINSTALL_DISABLED=true"):
            self.assertIn(off, grafana)

    def test_cleanup_removes_only_ours(self):
        listing = "\n".join([f"{box.LABEL}-dashboard-grafana-1\t999999999", "someone-else\t1",
                             f"{box.LABEL}-dashboard-datasette-2\t1"])
        calls = []

        def docker(*args, **_):
            calls.append(args)
            return listing if args[0] == "ps" else ""

        with mock.patch("agent.box.docker", docker), mock.patch("agent.box._alive", lambda o: o == "1"):
            dashboard.remove(only_dead=True)
        removed = {a[-1] for a in calls if a[0] == "rm"}
        self.assertEqual(removed, {f"{box.LABEL}-dashboard-grafana-1"})

    def test_bad_port(self):
        with self.assertRaises(ValueError):
            dashboard.main(["--grafana-port", "80"])

    def test_board_is_valid_json_and_read_only(self):
        b = json.loads(json.dumps(dashboard.board()))
        self.assertFalse(b["editable"])
        self.assertEqual(len({p["id"] for p in b["panels"]}), len(b["panels"]))
