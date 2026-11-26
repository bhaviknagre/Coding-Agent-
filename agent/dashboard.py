"""agent dashboard: see the event log in Grafana and Datasette, on this computer only.

Both read a copy of the log, made again every few seconds. The log itself is never opened for writing here.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from agent import box
from agent.log import START, digest
from agent.verify import data_folder

GRAFANA = "coding-agent-v2/grafana:11.3.0"  # built once from the image below, with the SQLite plugin
GRAFANA_BASE = "grafana/grafana-oss:11.3.0"
DATASETTE = "datasetteproject/datasette:0.65.1"
BUILD = f"""FROM {GRAFANA_BASE}
USER root
RUN mkdir /opt/plugins && grafana cli --pluginsDir /opt/plugins plugins install frser-sqlite-datasource 4.0.6 \\
 && a=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') \\
 && find /opt/plugins -name 'gpx_*' ! -name "*linux_${{a}}*" -delete \\
 && rm /opt/plugins/frser-sqlite-datasource/MANIFEST.txt && chown -R 472:0 /opt/plugins
USER 472
ENV GF_PATHS_PLUGINS=/opt/plugins
"""
KIND = "dashboard"
EVERY = 5  # seconds between copies of the log

VIEWS = """
create table integrity(checked real, events integer, state text);
create view tasks as
  select task,
    min(at) as started, (max(at) - min(at)) / 60.0 as minutes,
    case when sum(kind = 'verify.started') then 'agent verify' else 'agent' end as command,
    coalesce((select json_extract(body, '$.contract.what') from event s
       where s.task = e.task and s.kind = 'task.started' order by seq limit 1),
     (select 'commit ' || substr(json_extract(body, '$.result'), 1, 12) from event s
       where s.task = e.task and s.kind = 'verify.started' order by seq limit 1)) as request,
    (select json_extract(body, '$.model') from event s
       where s.task = e.task and s.kind = 'task.started' order by seq limit 1) as model,
    coalesce((select json_extract(body, '$.state') from event s
       where s.task = e.task and s.kind = 'task.ended' order by seq desc limit 1),
     (select coalesce(json_extract(body, '$.verdict'), 'not saved (older)') from event s
       where s.task = e.task and s.kind = 'verify.done' order by seq desc limit 1),
     (select 'CANCELLED' from event s
       where s.task = e.task and s.kind in ('task.cancelled', 'verify.cancelled') limit 1)) as verdict,
    sum(kind = 'model.asked') as model_calls, sum(kind = 'tool.asked') as tool_calls,
    sum(kind = 'tool.asked' and json_extract(body, '$.decision') = 'ASK') as asked_you,
    sum(kind = 'check.ended') as checks
  from event e group by task;
create view verdicts as
  select at, task, coalesce(json_extract(body, '$.state'), json_extract(body, '$.verdict')) as verdict
  from event where kind in ('task.ended', 'verify.done');
create view tool_calls as
  select at, task, json_extract(body, '$.tool') as tool, json_extract(body, '$.decision') as decision,
    json_extract(body, '$.rule') as rule from event where kind = 'tool.asked';
create view model_calls as
  select at, task, json_extract(body, '$.tokens') as tokens from event where kind = 'model.replied';
"""


def snapshot(log: Path, folder: Path) -> str:
    """Copy the log, add views for reading, check its hash chain. Returns the state of the chain."""
    folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = folder / ".events.db.new"
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        if log.exists():
            src = sqlite3.connect(f"file:{log}?mode=ro", uri=True, timeout=10)
            try:
                src.backup(out)
            finally:
                src.close()
        out.execute("pragma journal_mode=delete")
        out.execute(
            "create table if not exists event"
            "(seq integer primary key, at real, task text, kind text, body text, prev text, hash text)"
        )
        state, prev, count = "intact", START, 0
        for seq, at, task, kind, body, before, found in out.execute("select * from event order by seq"):
            count += 1
            if seq != count or before != prev or found != digest(prev, seq, at, task, kind, body):
                state = f"CHANGED at event {count}"
                break
            prev = found
        out.executescript(VIEWS)
        out.execute("insert into integrity values(?,?,?)", (time.time(), count, state))
        out.commit()
    finally:
        out.close()
    tmp.chmod(0o644)
    os.replace(tmp, folder / "events.db")
    return state


def _q(sql: str, kind: str = "table") -> dict[str, object]:
    return {"refId": "A", "datasource": {"type": "frser-sqlite-datasource", "uid": "agentlog"},
            "queryText": sql, "rawQueryText": sql, "queryType": kind, "timeColumns": ["time"]}


def _panel(
    n: int, title: str, kind: str, sql: str, x: int, y: int, w: int, h: int, **more: object
) -> dict[str, object]:
    q = _q(sql, "time series" if kind == "timeseries" else "table")
    return {"id": n, "title": title, "type": kind, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "datasource": q["datasource"], "targets": [q], **more}


def board() -> dict[str, object]:
    tz = time.localtime().tm_gmtoff  # seconds east of UTC
    stat = {"options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}, "colorMode": "value"}}
    count = "select count(*) as value from tasks"
    panels = [
        _panel(1, "Tasks", "stat", count, 0, 0, 4, 4, **stat),
        _panel(2, "VERIFIED", "stat", "select count(*) as value from tasks where verdict = 'VERIFIED'", 4, 0, 4, 4,
               **stat),
        _panel(3, "FAILED", "stat", "select count(*) as value from tasks where verdict = 'FAILED'", 8, 0, 4, 4, **stat),
        _panel(4, "UNVERIFIABLE or FLAKY", "stat",
               "select count(*) as value from tasks where verdict in ('UNVERIFIABLE', 'FLAKY')", 12, 0, 4, 4, **stat),
        _panel(5, "Questions to you", "stat", "select coalesce(sum(asked_you), 0) as value from tasks", 16, 0, 4, 4,
               **stat),
        _panel(6, "Log hash chain", "stat", "select state as value from integrity", 20, 0, 4, 4,
               options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"}, "textMode": "value"}),
        _panel(7, "Verdicts", "piechart",
               "select coalesce(verdict, 'not ended') as verdict, count(*) as tasks from tasks group by 1", 0, 4, 8, 8,
               options={"reduceOptions": {"calcs": ["sum"], "values": True, "fields": ""},
                        "legend": {"displayMode": "table", "placement": "right", "values": ["value"]}}),
        _panel(8, "Tool calls by tool", "barchart",
               "select tool, count(*) as calls from tool_calls group by tool order by calls desc", 8, 4, 8, 8),
        _panel(9, "Rule decisions (ALLOW, ASK, DENY)", "barchart",
               "select decision, count(*) as calls from tool_calls group by decision", 16, 4, 8, 8),
        _panel(10, "Model calls per hour", "timeseries",
               "select cast(at / 3600 as integer) * 3600 as time, count(*) as calls from model_calls group by 1 "
               "order by 1", 0, 12, 12, 8),
        _panel(11, "Tasks per hour, by verdict", "timeseries",
               "select cast(at / 3600 as integer) * 3600 as time, sum(verdict = 'VERIFIED') as verified, "
               "sum(verdict <> 'VERIFIED') as other from verdicts group by 1 order by 1", 12, 12, 12, 8),
        _panel(12, "Recent tasks", "table",
               f"select datetime(started + {tz}, 'unixepoch') as started, command, request, model, verdict, "  # noqa: S608 tz is a number
               "round(minutes, 1) as minutes, model_calls, tool_calls, asked_you, task "
               "from tasks order by started desc limit 30", 0, 20, 24, 9),
        _panel(13, "Recent events (the raw log)", "table",
               f"select seq, datetime(at + {tz}, 'unixepoch') as time_local, task, kind, substr(body, 1, 200) "  # noqa: S608 tz is a number
               "as body from event order by seq desc limit 100", 0, 29, 24, 10),
    ]
    return {"uid": "coding-agent", "title": "Coding agent", "refresh": "10s", "time": {"from": "now-7d", "to": "now"},
            "schemaVersion": 39, "editable": False, "panels": panels}


def files(folder: Path) -> None:
    """Grafana's settings: the log as a read-only data source, and the board above."""
    (folder / "provisioning" / "datasources").mkdir(parents=True, exist_ok=True)
    (folder / "provisioning" / "dashboards").mkdir(parents=True, exist_ok=True)
    (folder / "boards").mkdir(exist_ok=True)
    for empty in ("plugins", "alerting", "notifiers"):
        (folder / "provisioning" / empty).mkdir(exist_ok=True)
    (folder / "provisioning" / "datasources" / "log.yaml").write_text(json.dumps({
        "apiVersion": 1, "datasources": [{
            "name": "Agent log", "uid": "agentlog", "type": "frser-sqlite-datasource", "access": "proxy",
            "isDefault": True, "editable": False,
            "jsonData": {"path": "/data/events.db", "pathPrefix": "file:", "pathOptions": "mode=ro"}}]}))
    (folder / "provisioning" / "dashboards" / "boards.yaml").write_text(json.dumps({
        "apiVersion": 1, "providers": [{"name": "agent", "type": "file", "allowUiUpdates": False,
                                        "options": {"path": "/etc/grafana/boards"}}]}))
    (folder / "boards" / "agent.json").write_text(json.dumps(board(), indent=1))
    for p in folder.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)


def _name(part: str) -> str:
    return f"{box.LABEL}-{KIND}-{part}-{os.getpid()}"


def _common(part: str) -> list[str]:
    return ["run", "-d", "--name", _name(part), "--label", f"{box.LABEL}={KIND}",
            "--label", f"{box.LABEL}.owner={os.getpid()}", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "512m", "--pids-limit", "200", "--tmpfs", "/tmp"]  # noqa: S108 inside the container


def grafana_args(folder: Path, port: int) -> list[str]:
    settings = {
        "GF_AUTH_ANONYMOUS_ENABLED": "true", "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
        "GF_AUTH_DISABLE_LOGIN_FORM": "true", "GF_AUTH_BASIC_ENABLED": "false",
        "GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION": "true",
        "GF_ANALYTICS_REPORTING_ENABLED": "false", "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false", "GF_NEWS_NEWS_FEED_ENABLED": "false",
        # Unused copies for other processors are removed, which ends the plugin's signature: allowed by name.
        "GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS": "frser-sqlite-datasource",
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": "/etc/grafana/boards/agent.json",
    }
    env = [x for k, v in settings.items() for x in ("-e", f"{k}={v}")]
    return [*_common("grafana"), "--user", "472",
            "--tmpfs", "/var/lib/grafana:uid=472", "--tmpfs", "/var/log/grafana:uid=472",
            "-p", f"127.0.0.1:{port}:3000", *env,
            "-v", f"{folder / 'data'}:/data:ro", "-v", f"{folder / 'provisioning'}:/etc/grafana/provisioning:ro",
            "-v", f"{folder / 'boards'}:/etc/grafana/boards:ro", GRAFANA]


def datasette_args(folder: Path, port: int) -> list[str]:
    return [*_common("datasette"), "--user", "65534", "-p", f"127.0.0.1:{port}:8001",
            "-v", f"{folder / 'data'}:/data:ro", DATASETTE,
            "datasette", "serve", "/data/events.db", "-h", "0.0.0.0", "-p", "8001",  # noqa: S104 inside; published on 127.0.0.1 only
            "--setting", "sql_time_limit_ms", "3000", "--metadata", "/dev/null"]


def ours() -> list[str]:
    """Our dashboard containers: the label and the name must both match."""
    found = box.docker("ps", "-a", "--filter", f"label={box.LABEL}={KIND}", "--format",
                       '{{.Names}}\t{{.Label "' + box.LABEL + '.owner"}}')
    return [line for line in found.split("\n") if line.startswith(f"{box.LABEL}-{KIND}-")]


def remove(only_dead: bool) -> None:
    for line in ours():
        name, _, owner = line.partition("\t")
        if not only_dead or not box._alive(owner):
            box.docker("kill", name, ok=False)
            box.docker("rm", "-f", name, ok=False)


def _ready(url: str, seconds: float = 90) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with opener.open(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def _image(name: str) -> None:
    if box.docker("images", "-q", name):
        return
    print(f"  Getting {name} (once)…", flush=True)
    if name == GRAFANA:
        _image(GRAFANA_BASE)
        p = subprocess.run(
            ["docker", "build", "-q", "--label", f"{box.LABEL}={KIND}", "-t", GRAFANA, "-"],
            input=BUILD, text=True, capture_output=True, timeout=900)
        if p.returncode:
            raise box.BoxError("could not build the Grafana image: " + p.stderr.strip()[-300:])
    else:
        box.docker("pull", "-q", name, timeout=900)


def main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="agent dashboard", description="See the event log in your browser.")
    p.add_argument("--grafana-port", type=int, default=3000)
    p.add_argument("--datasette-port", type=int, default=8001)
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    for port in (a.grafana_port, a.datasette_port):
        if not 1024 <= port <= 65535:
            raise ValueError(f"port {port}: use one from 1024 to 65535")
    folder, log = data_folder() / "dashboard", data_folder() / "events.db"
    remove(only_dead=True)
    files(folder)
    state = snapshot(log, folder / "data")
    for name in (GRAFANA, DATASETTE):
        _image(name)
    grafana, datasette = f"http://127.0.0.1:{a.grafana_port}", f"http://127.0.0.1:{a.datasette_port}"
    try:
        for args, port, flag in ((grafana_args(folder, a.grafana_port), a.grafana_port, "--grafana-port"),
                                 (datasette_args(folder, a.datasette_port), a.datasette_port, "--datasette-port")):
            try:
                box.docker(*args)
            except box.BoxError as e:
                if "already in use" in str(e) or "not available" in str(e):
                    raise box.BoxError(f"port {port} is used by another program. Next: {flag} {port + 1}") from None
                raise
        print("  Starting…", flush=True)
        if not (_ready(grafana + "/api/health") and _ready(datasette + "/-/versions.json")):
            raise box.BoxError("the dashboard did not start. Next: is the port in use? Try --grafana-port 3001")
        print(flush=True)
        print(f"  Dashboard  {grafana}   (Grafana: charts)")
        print(f"  Data       {datasette}/events   (Datasette: every table, searchable)")
        print(f"  Log hash chain: {state}. Only this computer can open these pages; the log is copied every"
              f" {EVERY} s, never changed.\n  Ctrl+C to stop; nothing is left running.", flush=True)
        if not a.no_browser:
            webbrowser.open(grafana)
        while True:
            time.sleep(EVERY)
            snapshot(log, folder / "data")
    finally:
        remove(only_dead=False)
