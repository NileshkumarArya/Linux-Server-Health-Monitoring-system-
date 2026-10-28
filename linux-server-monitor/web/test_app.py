"""Tests for the web dashboard.   Run:  cd web && python3 -m unittest -v test_app
Uses a temporary data folder, so it never touches your real config, logs or reports."""
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ["SERVER_MONITOR_DATA_DIR"] = _TMP.name       # must be set before importing the app

import app as dash                                       # noqa: E402

H = {"X-Requested-With": "monitor-ui"}                   # header the page sends with every POST
KEYS = ("cpu", "ram", "disk")


class Base(unittest.TestCase):
    def setUp(self):
        self.c = dash.app.test_client()
        dash.save_thresholds(dict(dash.DEFAULTS))

    def post(self, path, body=None, headers=H):
        return self.c.post(path, json=body, headers=headers)


class Metrics(Base):
    def test_payload_shape_and_values(self):
        r = self.c.get("/api/metrics")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for k in ("cpu", "mem", "disk", "load", "uptime", "os", "procs", "users", "net", "disks",
                  "thresholds", "alerts", "warnings", "ts"):
            self.assertIn(k, d)
        self.assertTrue(0 <= d["cpu"]["percent"] <= 100)
        self.assertTrue(0 <= d["mem"]["percent"] <= 100)
        self.assertTrue(0 <= d["disk"]["percent"] <= 100)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_alert_rule_is_greater_or_equal(self):
        self.assertEqual(dash.status(80, 80), "WARNING")      # equal counts as a warning
        self.assertEqual(dash.status(79.9, 80), "OK")
        self.assertEqual(dash.status(None, 80), "UNAVAILABLE")

    def test_process_sampler_reports_real_processes(self):
        deadline = time.time() + 8
        procs = []
        while time.time() < deadline and not procs:
            procs = self.c.get("/api/metrics").get_json()["procs"]
            time.sleep(0.5)
        self.assertTrue(procs, "sampler produced no process rows")
        self.assertLessEqual(len(procs), 10)
        self.assertEqual(sorted(procs, key=lambda p: (p["cpu"], p["mem"]), reverse=True), procs)
        self.assertTrue({"pid", "user", "cpu", "mem", "cmd"} <= set(procs[0]))

    def test_command_names_with_spaces_survive(self):
        line = "123 (tmux: server) S " + " ".join(["0"] * 10 + ["5", "7"] + ["0"] * 8 + ["42"] + ["0"] * 20)
        with mock.patch.object(dash, "_read", return_value=line), \
             mock.patch.object(dash.os, "listdir", return_value=["123"]), \
             mock.patch.object(dash.os, "stat", return_value=mock.Mock(st_uid=0)):
            self.assertEqual(dash._proc_counters()[123][:2], (12, "tmux: server"))


class Csrf(Base):
    def test_post_without_header_is_forbidden(self):
        for path in ("/api/thresholds", "/api/service", "/api/report"):
            self.assertEqual(self.post(path, {"cpu": 50}, headers={}).status_code, 403, path)

    def test_forbidden_does_not_change_anything(self):
        self.post("/api/thresholds", {"cpu": 50}, headers={})
        self.assertEqual(dash.load_thresholds()["cpu"], 80)


class Thresholds(Base):
    def test_valid_values_saved_and_bash_compatible(self):
        r = self.post("/api/thresholds", {"cpu": 70, "ram": "75", "disk": 100})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["thresholds"], {"cpu": 70, "ram": 75, "disk": 100})
        text = (Path(_TMP.name) / "config.conf").read_text()
        for line in ("CPU_THRESHOLD=70", "RAM_THRESHOLD=75", "DISK_THRESHOLD=100"):
            self.assertIn(line, text.splitlines())

    def test_invalid_values_rejected_and_nothing_changes(self):
        for bad in (0, 101, -5, 5.5, "abc", "", None, True, "1e2", "007", " ", [1], {"a": 1}):
            r = self.post("/api/thresholds", {"cpu": bad})
            self.assertEqual(r.status_code, 400, repr(bad))
        self.assertEqual(dash.load_thresholds(), dash.DEFAULTS)

    def test_partial_failure_is_all_or_nothing(self):
        self.assertEqual(self.post("/api/thresholds", {"cpu": 60, "ram": 500}).status_code, 400)
        self.assertEqual(dash.load_thresholds()["cpu"], 80)

    def test_empty_or_unknown_body_rejected(self):
        self.assertEqual(self.post("/api/thresholds", {}).status_code, 400)
        self.assertEqual(self.post("/api/thresholds", {"foo": 1}).status_code, 400)
        self.assertEqual(self.post("/api/thresholds", None).status_code, 400)

    def test_corrupt_config_falls_back_to_defaults_per_key(self):
        (Path(_TMP.name) / "config.conf").write_text(
            "CPU_THRESHOLD=$(touch /tmp/pwned)\nRAM_THRESHOLD=150\nDISK_THRESHOLD=55\n`id`\n")
        dash._cfg["mtime"] = None                                  # force a re-read
        self.assertEqual(dash.load_thresholds(), {"cpu": 80, "ram": 80, "disk": 55})
        self.assertFalse(os.path.exists("/tmp/pwned"))

    def test_changes_picked_up_when_bash_tool_edits_the_file(self):
        time.sleep(0.01)
        (Path(_TMP.name) / "config.conf").write_text("CPU_THRESHOLD=33\nRAM_THRESHOLD=44\nDISK_THRESHOLD=55\n")
        self.assertEqual(dash.load_thresholds(), {"cpu": 33, "ram": 44, "disk": 55})


class Service(Base):
    SHOW = "LoadState=loaded\nActiveState={a}\nSubState=dead\nUnitFileState={e}\nDescription=Demo service"

    def systemd(self, out, rc=0):
        return mock.patch.multiple(dash, SYSTEMD_DIR=Path(_TMP.name), _run=mock.Mock(return_value=(rc, out)),
                                   shutil=mock.Mock(which=mock.Mock(return_value="/bin/systemctl")))

    def test_invalid_names_rejected(self):
        for bad in ("bad name;rm -rf /", "-x", "$(id)", "a|b", "a\nb", "x" * 129, "../etc", "a b"):
            self.assertEqual(self.post("/api/service", {"name": bad}).get_json().get("error"),
                             "Invalid service name", repr(bad))

    def test_empty_name(self):
        self.assertEqual(self.post("/api/service", {"name": "  "}).status_code, 400)

    def test_valid_names_pass_validation(self):
        for ok in ("ssh", "cron.service", "getty@tty1", "systemd-resolved", "a.b_c:d-e"):
            self.assertTrue(dash._SERVICE_RE.fullmatch(ok), ok)

    def test_stopped_service_shows_inactive_not_unknown(self):       # regression: is-active exits non-zero
        with self.systemd(self.SHOW.format(a="inactive", e="disabled")):
            d = self.post("/api/service", {"name": "demo"}).get_json()
        self.assertEqual((d["active"], d["enabled"], d["error"]), ("inactive", "disabled", None))

    def test_running_service(self):
        with self.systemd(self.SHOW.format(a="active", e="enabled")):
            d = self.post("/api/service", {"name": "ssh"}).get_json()
        self.assertEqual((d["active"], d["enabled"], d["description"]), ("active", "enabled", "Demo service"))

    def test_unknown_service(self):
        with self.systemd("LoadState=not-found\nActiveState=inactive"):
            self.assertIn("not found", self.post("/api/service", {"name": "ghost"}).get_json()["error"])

    def test_no_systemd(self):
        with mock.patch.object(dash, "SYSTEMD_DIR", Path(_TMP.name) / "missing"):
            self.assertIn("systemd", self.post("/api/service", {"name": "ssh"}).get_json()["error"])


class Reports(Base):
    def test_generate_list_view_download(self):
        r = self.post("/api/report")
        self.assertEqual(r.status_code, 200)
        name = r.get_json()["filename"]
        self.assertRegex(name, r"^report_\d{8}_\d{6}(_\d+)?\.txt$")
        listed = self.c.get("/api/reports").get_json()
        self.assertIn(name, [f["name"] for f in listed])
        view = self.c.get(f"/api/reports/{name}?view=1")
        self.assertEqual(view.status_code, 200)
        self.assertIn("LINUX SERVER HEALTH REPORT", view.get_data(as_text=True))
        self.assertNotIn("attachment", view.headers.get("Content-Disposition", ""))
        dl = self.c.get(f"/api/reports/{name}")
        self.assertIn("attachment", dl.headers["Content-Disposition"])

    def test_same_second_reports_never_overwrite(self):
        real = time.strftime
        fake = lambda fmt, *a: "20260101_101010" if fmt == "%Y%m%d_%H%M%S" else real(fmt, *a)
        with mock.patch.object(dash.time, "strftime", side_effect=fake):
            names = {self.post("/api/report").get_json()["filename"] for _ in range(3)}
        self.assertEqual(names, {"report_20260101_101010.txt", "report_20260101_101010_1.txt",
                                 "report_20260101_101010_2.txt"})

    def test_bad_names_rejected(self):
        for bad in ("evil.txt", "report_1.txt", "report_20260101_101010.txt.bak", "..%2Fapp.py", "report_20260101_101010.TXT"):
            self.assertIn(self.c.get(f"/api/reports/{bad}").status_code, (400, 404), bad)
        self.assertEqual(self.c.get("/api/reports/report_20260101_000000.txt").status_code, 404)

    def test_report_contents_match_alert_rule(self):
        self.post("/api/thresholds", {"cpu": 100, "ram": 100, "disk": 1})
        name = self.post("/api/report").get_json()["filename"]
        text = self.c.get(f"/api/reports/{name}?view=1").get_data(as_text=True)
        self.assertRegex(text, r"Disk : WARNING")


class Logs(Base):
    def test_actions_are_logged_in_bash_compatible_format(self):
        self.post("/api/thresholds", {"cpu": 61})
        lines = self.c.get("/api/logs").get_json()
        self.assertTrue(lines)
        self.assertRegex(lines[-1], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[(INFO|WARN|ERROR)\] ")

    def test_log_rotation(self):
        with open(dash.LOG_FILE, "a") as f:
            f.write("x" * (dash.LOG_MAX_BYTES + 10))
        dash.log("INFO", "after rotation")
        self.assertTrue(dash.LOG_FILE.with_name(dash.LOG_FILE.name + ".1").exists())
        self.assertIn("after rotation", dash.tail_log(5)[-1])

    def test_newlines_cannot_forge_log_lines(self):
        dash.log("INFO", "line one\n2026-01-01 00:00:00 [ERROR] forged")
        self.assertEqual(len([l for l in dash.tail_log(5) if "forged" in l]), 1)
        self.assertNotRegex(dash.tail_log(1)[-1].split("] ", 1)[1], r"^\d{4}-")


class Pages(Base):
    def test_index_and_error_shapes(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        r = self.c.get("/api/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())
        self.assertEqual(self.c.post("/api/metrics").status_code, 403)

    def test_page_has_csp_and_escaping(self):
        r = self.c.get("/")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertIn("const esc=", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
