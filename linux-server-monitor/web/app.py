#!/usr/bin/env python3
"""
Linux Server Health Monitor - Web Dashboard  (v1.1)

Single-file Flask application: JSON API + embedded HTML/CSS/JS dashboard.
Reads real data from /proc and standard utilities (df, who, systemctl).
Read-only monitoring: it never restarts services or kills processes.

Run     :  python3 web/app.py                  (http://127.0.0.1:5000)
Network :  python3 web/app.py --host 0.0.0.0   (no login - use only on a trusted network)
Tests   :  cd web && python3 -m unittest -v test_app
"""
import argparse
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

VERSION = "1.1"
app = Flask(__name__)
os.umask(0o077)                      # files are private to the current user (rw-------)

# ── storage ───────────────────────────────────────────────────────────────────

def _pick_data_dir():
    """Same data folder as server_monitor.sh (project_root/data) so both tools
    share config, logs and reports. SERVER_MONITOR_DATA_DIR overrides it."""
    env = os.environ.get("SERVER_MONITOR_DATA_DIR")
    here = Path(__file__).resolve().parent
    candidates = [Path(env)] if env else [here.parent / "data", here / "data"]
    for c in candidates:
        try:
            (c / "reports").mkdir(parents=True, exist_ok=True)
            (c / "logs").mkdir(parents=True, exist_ok=True)
            if os.access(c, os.W_OK):
                return c
        except OSError:
            continue
    raise SystemExit("Cannot create a writable data directory (tried: %s)"
                     % ", ".join(str(c) for c in candidates))

DATA_DIR = _pick_data_dir()
REPORT_DIR = DATA_DIR / "reports"
LOG_FILE = DATA_DIR / "logs" / "app.log"
CONF_FILE = DATA_DIR / "config.conf"
LOG_MAX_BYTES = 1024 * 1024          # rotate app.log to app.log.1 above 1 MiB

KEYS = ("cpu", "ram", "disk")
DEFAULTS = {"cpu": 80, "ram": 80, "disk": 80}
DISK_PATH = "/"
SYSTEMD_DIR = Path("/run/systemd/system")

_CONF_LINE = re.compile(r"(CPU|RAM|DISK)_THRESHOLD=([1-9][0-9]{0,2})")
_SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:\-]{0,127}")
SERVICE_PROPS = "LoadState,ActiveState,SubState,UnitFileState,Description"
_REPORT_RE = re.compile(r"report_[0-9]{8}_[0-9]{6}(_[0-9]+)?\.txt")

_cfg_lock = threading.Lock()
_cfg = {"mtime": None, "data": dict(DEFAULTS)}


def _write_conf(t):
    tmp = CONF_FILE.with_name(CONF_FILE.name + ".tmp")
    tmp.write_text("# Linux Server Monitor settings (whole numbers 1-100)\n"
                   f"CPU_THRESHOLD={t['cpu']}\nRAM_THRESHOLD={t['ram']}\nDISK_THRESHOLD={t['disk']}\n")
    tmp.replace(CONF_FILE)           # atomic: the old file stays intact if writing fails


def _mtime():
    try:
        return CONF_FILE.stat().st_mtime_ns
    except OSError:
        return None


def load_thresholds():
    """Thresholds from config.conf (re-read only when the file changes).
    Lines must be exactly KEY=NUMBER (1-100), the same rule the Bash tool uses."""
    with _cfg_lock:
        m = _mtime()
        if m is None or m != _cfg["mtime"]:
            t = dict(DEFAULTS)
            if m is None:
                try:
                    _write_conf(t)           # first run: create the file with defaults
                except OSError:
                    pass
                m = _mtime()
            else:
                try:
                    for line in CONF_FILE.read_text().splitlines():
                        hit = _CONF_LINE.fullmatch(line.strip())
                        if hit and int(hit.group(2)) <= 100:
                            t[hit.group(1).lower()] = int(hit.group(2))
                except (OSError, UnicodeDecodeError):
                    pass
            _cfg["data"], _cfg["mtime"] = t, m
        return dict(_cfg["data"])


def save_thresholds(t):
    with _cfg_lock:
        _write_conf(t)
        _cfg["data"], _cfg["mtime"] = dict(t), _mtime()


def log(level, msg):
    """Append 'YYYY-MM-DD HH:MM:SS [LEVEL] message' - the same format as the Bash tool."""
    try:
        msg = str(msg).replace("\n", " ").replace("\r", " ")[:300]
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n")
    except OSError:
        pass


def tail_log(n=30):
    """Last n lines without reading the whole file."""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            return f.read().decode("utf8", "replace").splitlines()[-n:]
    except OSError:
        return []

# ── helpers ───────────────────────────────────────────────────────────────────
_ENV = {**os.environ, "LC_ALL": "C"}          # stable output format for df, who, systemctl


def _run(args, timeout=5):
    """Run a command without a shell. Returns (exit_code, stdout)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_ENV)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _cmd(args, timeout=5):
    rc, out = _run(args, timeout)
    return out if rc == 0 else ""


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


_ttl = {}


def cached(key, seconds, fn):
    """Tiny time-based cache for slow, slowly-changing data (df, who)."""
    now = time.monotonic()
    hit = _ttl.get(key)
    if hit and now - hit[0] < seconds:
        return hit[1]
    value = fn()
    _ttl[key] = (now, value)
    return value

# ── background sampler: CPU, network and per-process CPU ──────────────────────
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_user_names = {}


def _user_name(uid):
    if uid not in _user_names:
        try:
            _user_names[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _user_names[uid] = str(uid)
    return _user_names[uid]


def _cpu_counters():
    parts = _read("/proc/stat").split("\n", 1)[0].split()
    if not parts or parts[0] != "cpu":
        return None
    v = [int(x) for x in parts[1:9] if x.isdigit()]
    v += [0] * (8 - len(v))
    return sum(v), v[3] + v[4]                  # total, idle(+iowait)


def _net_counters():
    out = {}
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name, cols = name.strip(), rest.split()
        if name != "lo" and len(cols) >= 9:
            out[name] = (int(cols[0]), int(cols[8]))
    return out


def _proc_counters():
    """pid -> (cpu ticks, command name, uid, resident pages) from /proc/<pid>/stat."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            data = _read(f"/proc/{d}/stat")
            r = data.rfind(")")                  # the command name may contain spaces or ")"
            name = data[data.find("(") + 1:r]
            rest = data[r + 2:].split()
            out[int(d)] = (int(rest[11]) + int(rest[12]), name,
                           os.stat(f"/proc/{d}").st_uid, int(rest[21]))
        except (OSError, IndexError, ValueError):
            continue                             # the process ended while we were reading
    return out


class Sampler(threading.Thread):
    """Samples once per second so an HTTP request never has to wait for a measurement."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.cpu = 0.0
        self.net = []
        self.procs = []
        self._cpu = self._net = self._procs = None
        self._t_net = self._t_proc = None
        self._tick = 0

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as exc:             # a sampling problem must never kill the thread
                log("ERROR", f"sampler: {exc}")
            time.sleep(1.0)

    def sample(self):
        now = time.monotonic()
        cpu = _cpu_counters()
        if cpu and self._cpu:
            dt = cpu[0] - self._cpu[0]
            di = cpu[1] - self._cpu[1]
            if dt > 0:
                busy = (dt - di) * 100.0 / dt
                with self.lock:
                    self.cpu = round(max(0.0, min(100.0, busy)), 1)
                self.ready.set()
        self._cpu = cpu

        net = _net_counters()
        if self._net and now > self._t_net:
            span = now - self._t_net
            rows = [{"name": n, "rx_mb": round(rx / 1048576, 1), "tx_mb": round(tx / 1048576, 1),
                     "rx_kbs": round(max(0, rx - self._net.get(n, (rx, tx))[0]) / 1024 / span, 1),
                     "tx_kbs": round(max(0, tx - self._net.get(n, (rx, tx))[1]) / 1024 / span, 1)}
                    for n, (rx, tx) in sorted(net.items())]
            with self.lock:
                self.net = rows
        self._net, self._t_net = net, now

        self._tick += 1
        if self._tick % 2 == 0:                  # per-process CPU every 2 seconds
            procs = _proc_counters()
            if self._procs and now > self._t_proc:
                span = now - self._t_proc
                mem_total = _meminfo().get("MemTotal", 0) * 1024
                rows = []
                for pid, (ticks, name, uid, rss) in procs.items():
                    prev = self._procs.get(pid)
                    if not prev:
                        continue
                    cpu_pct = max(0.0, (ticks - prev[0]) / CLK_TCK / span * 100.0)
                    mem_pct = rss * PAGE_SIZE * 100.0 / mem_total if mem_total else 0.0
                    rows.append({"pid": pid, "user": _user_name(uid), "cpu": round(cpu_pct, 1),
                                 "mem": round(mem_pct, 1), "cmd": name})
                rows.sort(key=lambda r: (r["cpu"], r["mem"]), reverse=True)
                with self.lock:
                    self.procs = rows[:10]
            self._procs, self._t_proc = procs, now

    def read(self):
        with self.lock:
            return self.cpu, list(self.net), list(self.procs)


sampler = Sampler()
sampler.start()

# ── collectors ────────────────────────────────────────────────────────────────

def _meminfo():
    kv = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            kv[parts[0].rstrip(":")] = int(parts[1])
    return kv


def mem_info():
    kv = _meminfo()
    total = kv.get("MemTotal", 0)
    avail = kv.get("MemAvailable", kv.get("MemFree", 0)
                   + kv.get("Buffers", 0) + kv.get("Cached", 0))
    avail = min(avail, total)
    used = total - avail
    return {"total_mb": round(total / 1024, 1),
            "used_mb": round(used / 1024, 1),
            "free_mb": round(avail / 1024, 1),
            "percent": round(used * 100 / total, 1) if total else None}


def disk_info():
    """Usage of DISK_PATH from `df -P -k`; the percentage is df's own Use%."""
    lines = _cmd(["df", "-P", "-k", DISK_PATH]).splitlines()
    parts = lines[-1].split(None, 5) if len(lines) >= 2 else []
    if len(parts) < 6 or not parts[4].endswith("%") or not parts[1].isdigit():
        return {"total_gb": None, "used_gb": None, "free_gb": None, "percent": None,
                "fs": "?", "mount": DISK_PATH}
    fs, total, used, free, pct, mount = parts
    gb = lambda kb: round(int(kb) / 1048576, 1)
    return {"total_gb": gb(total), "used_gb": gb(used), "free_gb": gb(free),
            "percent": int(pct[:-1]), "fs": fs, "mount": mount}


def all_disks():
    out = _cmd(["df", "-P", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x", "overlay"])
    rows = []
    for line in out.splitlines()[1:]:
        p = line.split(None, 5)
        if len(p) == 6:
            rows.append({"fs": p[0], "size": p[1], "used": p[2], "avail": p[3], "pct": p[4], "mount": p[5]})
    return rows


def logged_users():
    users = []
    for line in _cmd(["who"]).splitlines():
        p = line.split()
        if len(p) >= 2:
            users.append({"user": p[0], "tty": p[1], "from": p[-1].strip("()") if p[-1].startswith("(") else "local"})
    return users


def load_avg():
    raw = _read("/proc/loadavg").split()
    return {"m1": raw[0], "m5": raw[1], "m15": raw[2]} if len(raw) >= 3 else {}


def uptime_str():
    try:
        secs = int(float(_read("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        return "?"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    return " ".join(([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + [f"{r // 60}m"])


@lru_cache(maxsize=1)
def static_info():
    """Values that cannot change while the server runs."""
    osr = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k.strip()] = v.strip().strip('"')
    model = next((l.split(":", 1)[1].strip() for l in _read("/proc/cpuinfo").splitlines()
                  if l.startswith("model name")), "Unknown")
    un = os.uname()
    return {"os": {"name": osr.get("PRETTY_NAME", "Linux"), "kernel": un.release, "host": un.nodename},
            "cpu": {"model": model, "cores": os.cpu_count() or 1}}


def status(value, threshold):
    """OK / WARNING / UNAVAILABLE - WARNING when value >= threshold."""
    if value is None:
        return "UNAVAILABLE"
    return "WARNING" if value >= threshold else "OK"


def snapshot():
    sampler.ready.wait(3)
    cpu, net, procs = sampler.read()
    t, mem = load_thresholds(), mem_info()
    dk, info = disk_info(), static_info()
    alerts = {"cpu":  status(cpu, t["cpu"]),
              "ram":  status(mem["percent"], t["ram"]),
              "disk": status(dk["percent"], t["disk"])}
    return {"version": VERSION,
            "cpu": {"percent": cpu, **info["cpu"]},
            "mem": mem, "disk": dk, "load": load_avg(),
            "uptime": uptime_str(), "os": info["os"],
            "procs": procs, "net": net,
            "users": cached("users", 5, logged_users),
            "disks": cached("disks", 10, all_disks),
            "thresholds": t, "alerts": alerts,
            "warnings": sum(v == "WARNING" for v in alerts.values()),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


def check_service(name):
    """Read-only service status through one `systemctl show` call."""
    if not _SERVICE_RE.fullmatch(name):
        return {"error": "Invalid service name"}
    if not shutil.which("systemctl") or not SYSTEMD_DIR.exists():
        return {"error": "systemd is not running on this machine"}
    rc, out = _run(["systemctl", "show", "--property=" + SERVICE_PROPS,
                    "--", name])      # read-only: show, never start/stop
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if rc != 0 or props.get("LoadState") == "not-found":
        return {"error": f"Service '{name}' not found"}
    result = {"name": name, "active": props.get("ActiveState") or "unknown",
              "sub": props.get("SubState", ""), "enabled": props.get("UnitFileState") or "unknown",
              "description": props.get("Description", ""), "error": None}
    log("INFO", f"Service check: {name} active={result['active']} enabled={result['enabled']}")
    return result


def build_report(snap):
    t, a, cpu, mem, dk, la = snap["thresholds"], snap["alerts"], snap["cpu"], snap["mem"], snap["disk"], snap["load"]
    pct = lambda v: "N/A" if v is None else f"{v}%"
    lines = [
        "LINUX SERVER HEALTH REPORT (Web Dashboard)", "=" * 50,
        f"Generated : {snap['ts']}", "",
        "[SYSTEM]",
        f"  Hostname  : {snap['os']['host']}", f"  OS        : {snap['os']['name']}",
        f"  Kernel    : {snap['os']['kernel']}", f"  Uptime    : {snap['uptime']}", "",
        "[CPU]",
        f"  Model     : {cpu['model']}", f"  Cores     : {cpu['cores']}", f"  Usage     : {pct(cpu['percent'])}",
        f"  Load avg  : {la.get('m1', '?')} (1m)  {la.get('m5', '?')} (5m)  {la.get('m15', '?')} (15m)", "",
        "[MEMORY]",
        f"  Total     : {mem['total_mb']} MB", f"  Used      : {mem['used_mb']} MB",
        f"  Available : {mem['free_mb']} MB", f"  Usage     : {pct(mem['percent'])}", "",
        f"[DISK - filesystem holding {DISK_PATH}]",
        f"  Filesystem: {dk['fs']}", f"  Size      : {dk['total_gb']} GB", f"  Used      : {dk['used_gb']} GB",
        f"  Available : {dk['free_gb']} GB", f"  Usage     : {pct(dk['percent'])}", "",
        f"[ALERTS]  (thresholds: CPU={t['cpu']}%  RAM={t['ram']}%  Disk={t['disk']}%)",
        f"  CPU  : {a['cpu']}  ({pct(cpu['percent'])})",
        f"  RAM  : {a['ram']}  ({pct(mem['percent'])})",
        f"  Disk : {a['disk']}  ({pct(dk['percent'])})", "",
        "[TOP PROCESSES BY CPU]  (CPU % measured over the last few seconds)",
    ]
    lines += [f"  {p['pid']:>7}  {p['user']:<12}  CPU:{p['cpu']:>5}%  MEM:{p['mem']:>5}%  {p['cmd']}" for p in snap["procs"]] \
        or ["  (no data yet)"]
    lines += ["", "[LOGGED-IN USERS]"]
    lines += [f"  {u['user']}  {u['tty']}  {u['from']}" for u in snap["users"]] or ["  No users logged in."]
    return "\n".join(lines + ["", "--- end of report ---"]) + "\n"

# ── security: CSRF guard, headers, JSON errors ───────────────────────────────

@app.before_request
def _csrf_guard():
    # A cross-site page cannot send this header (this app allows no CORS).
    ok = request.headers.get("X-Requested-With") == "monitor-ui"
    if request.method == "POST" and not ok:
        abort(403)


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'unsafe-inline'; "
                                               "style-src 'unsafe-inline'; connect-src 'self'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(HTTPException)
def _http_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.description if err.code != 403 else "Forbidden"}), err.code
    return err

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/metrics")
def api_metrics():
    return jsonify(snapshot())


@app.route("/api/service", methods=["POST"])
def api_service():
    body = request.get_json(silent=True) or {}
    # never truncated: a 129-character name is simply invalid
    name = str(body.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Service name required"}), 400
    return jsonify(check_service(name))


@app.route("/api/thresholds", methods=["GET"])
def api_thresholds_get():
    return jsonify(load_thresholds())


@app.route("/api/thresholds", methods=["POST"])
def api_thresholds_set():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not any(k in data for k in KEYS):
        return jsonify({"error": "Send at least one of: cpu, ram, disk"}), 400
    t, errors = load_thresholds(), []
    for key in KEYS:
        if key not in data:
            continue
        raw = data[key]
        # whole numbers only: 1-100 (no decimals, signs, booleans or text)
        if isinstance(raw, bool) or not re.fullmatch(r"[1-9][0-9]{0,2}", str(raw).strip()) or int(str(raw).strip()) > 100:
            errors.append(f"{key}: must be a whole number from 1 to 100")
        else:
            t[key] = int(str(raw).strip())
    if errors:
        log("WARN", "Invalid threshold rejected: " + "; ".join(errors))
        return jsonify({"error": "; ".join(errors)}), 400
    try:
        save_thresholds(t)
    except OSError as exc:
        log("ERROR", f"Could not save thresholds: {exc}")
        return jsonify({"error": "Could not save thresholds (check folder permissions)"}), 500
    log("INFO", f"Thresholds updated: CPU={t['cpu']} RAM={t['ram']} Disk={t['disk']}")
    return jsonify({"ok": True, "thresholds": t})


@app.route("/api/report", methods=["POST"])
def api_report():
    text = build_report(snapshot())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    n = 0
    while True:                                   # O_EXCL: never overwrite an earlier report
        name = f"report_{stamp}.txt" if n == 0 else f"report_{stamp}_{n}.txt"
        try:
            fd = os.open(REPORT_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
        except OSError as exc:
            log("ERROR", f"Report not saved: {exc}")
            return jsonify({"error": "Could not save the report (check folder permissions)"}), 500
    with os.fdopen(fd, "w") as f:
        f.write(text)
    log("INFO", f"Report generated: {name}")
    return jsonify({"ok": True, "filename": name})


@app.route("/api/reports")
def api_reports():
    files = sorted((p for p in REPORT_DIR.glob("report_*.txt") if _REPORT_RE.fullmatch(p.name)),
                   key=lambda p: p.name, reverse=True)[:20]
    return jsonify([{"name": p.name, "size": p.stat().st_size,
                     "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))} for p in files])


@app.route("/api/reports/<filename>")
def api_report_file(filename):
    if not _REPORT_RE.fullmatch(filename):
        abort(400, "Invalid report name")
    if not (REPORT_DIR / filename).is_file():
        abort(404, "Report not found")
    inline = request.args.get("view") == "1"
    return send_from_directory(REPORT_DIR, filename, mimetype="text/plain", as_attachment=not inline)


@app.route("/api/logs")
def api_logs():
    return jsonify(tail_log(30))


@app.route("/")
def index():
    return PAGE

# ── HTML / CSS / JS (single page) ─────────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Server Monitor</title>
<style>
:root{--bg:#0b1220;--panel:#111c30;--panel2:#0d1627;--border:#1e2f47;--accent:#10b981;--warn:#f59e0b;
  --danger:#ef4444;--sky:#38bdf8;--muted:#8092a8;--text:#e2e8f0;--dim:#9fb0c4;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono","Courier New",monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;min-height:100vh}
button,input{font-family:inherit}
:focus-visible{outline:2px solid var(--sky);outline-offset:2px}
header{display:flex;align-items:center;justify-content:space-between;gap:10px 18px;flex-wrap:wrap;
  padding:12px 24px;border-bottom:1px solid var(--border);background:var(--panel);position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent)}
.dot.off{background:var(--danger);box-shadow:0 0 8px var(--danger)}
.brand b{font-size:15px;letter-spacing:.5px;color:#fff;display:block}
.brand small{font-size:10px;color:var(--dim)}
.meta{display:flex;align-items:center;gap:6px 16px;font-size:11px;color:var(--dim);flex-wrap:wrap}
.meta b{color:var(--text)}
.pill{padding:3px 11px;border-radius:20px;font-weight:700;font-size:11px;border:1px solid}
.pill.ok{color:var(--accent);background:#0a2318;border-color:#134e35}
.pill.bad{color:var(--danger);background:#2a0808;border-color:#7c1212}
.pill.na{color:var(--warn);background:#2a1c00;border-color:#78450a}
nav{display:flex;border-bottom:1px solid var(--border);background:var(--panel);padding:0 24px;overflow-x:auto}
.tab{padding:10px 18px;cursor:pointer;font-size:12px;color:var(--dim);background:none;border:0;
  border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent)}
.view{display:none;padding:20px 24px}
.view.active{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}
.wide{grid-column:1/-1}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px 20px;min-width:0}
.card h3{font-size:10px;font-weight:400;color:var(--dim);letter-spacing:.8px;text-transform:uppercase;margin-bottom:14px}
.big{font-size:46px;font-weight:700;line-height:1;letter-spacing:-1px}
.sub{font-size:11px;color:var(--dim);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gauge{position:relative;display:flex;align-items:center;gap:10px;margin-top:12px}
.track{position:relative;flex:1;height:6px;border-radius:3px;background:var(--border)}
.fill{height:100%;border-radius:3px;transition:width .5s,background .5s;width:0}
.tick{position:absolute;top:-4px;width:2px;height:14px;background:var(--text);opacity:.55;border-radius:1px}
.glabel{font-size:11px;color:var(--dim);white-space:nowrap;min-width:48px;text-align:right}
canvas{width:100%;height:46px;display:block;margin-top:10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid var(--border)}
.row:last-child{border-bottom:0}
.k{color:var(--dim)}.v{font-weight:600;text-align:right;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend{font-size:10px;color:var(--muted);margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--dim);font-weight:400;padding:0 8px 8px;letter-spacing:.5px;text-transform:uppercase;font-size:10px}
td{padding:7px 8px;border-top:1px solid var(--border);overflow-wrap:anywhere}
.scroll{overflow-x:auto}
.bar{display:flex;align-items:center;gap:6px}.bar .track{height:4px}.bar span{min-width:44px;text-align:right}
.alert{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:8px;margin-bottom:8px;font-size:12px;border:1px solid}
.alert:last-of-type{margin-bottom:0}
.alert.ok{background:#0a2318;border-color:#134e35}.alert.bad{background:#2a0808;border-color:#7c1212}.alert.na{background:#2a1c00;border-color:#78450a}
.ok-c{color:var(--accent)}.bad-c{color:var(--danger)}.warn-c{color:var(--warn)}.sky-c{color:var(--sky)}.mut-c{color:var(--muted)}
input[type=text],input[type=number]{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 12px;
  border-radius:6px;font-size:13px;width:100%;outline:none}
input:focus{border-color:var(--accent)}
label.f{display:flex;align-items:center;gap:10px;margin-bottom:12px}
label.f span{color:var(--dim);font-size:11px;min-width:84px}
.btn{padding:8px 18px;border-radius:6px;border:0;cursor:pointer;font-size:12px;font-weight:700}
.btn:hover{opacity:.88}.btn:disabled{opacity:.5;cursor:wait}
.green{background:var(--accent);color:#04130d}.skyb{background:var(--sky);color:#04131c}.grey{background:var(--border);color:var(--text)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 4px}
.chip{background:var(--bg);border:1px solid var(--border);color:var(--dim);border-radius:20px;padding:3px 12px;font-size:11px;cursor:pointer}
.chip:hover{color:var(--text);border-color:var(--sky)}
.msg{padding:8px 12px;border-radius:6px;font-size:12px;margin-top:12px;display:none;border:1px solid}
.msg.ok{display:block;background:#0a2318;border-color:#134e35;color:var(--accent)}
.msg.bad{display:block;background:#2a0808;border-color:#7c1212;color:var(--danger)}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;border:1px solid}
.badge.ok{background:#0a2318;color:var(--accent);border-color:#134e35}.badge.bad{background:#2a0808;color:var(--danger);border-color:#7c1212}
.badge.na{background:#2a1c00;color:var(--warn);border-color:#78450a}
.item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-radius:6px;margin-bottom:6px;
  background:var(--bg);border:1px solid var(--border)}
.item .n{color:var(--sky);font-size:12px;overflow-wrap:anywhere}.item .s{color:var(--muted);font-size:10px}
pre.box{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:340px;overflow:auto;
  font-size:11px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}
.ln-w{color:var(--warn)}.ln-e{color:var(--danger)}.ln-i{color:var(--dim)}
.title{font-size:14px;font-weight:700;margin-bottom:14px}
.hint{font-size:11px;color:var(--muted);margin-top:8px}
#offline{display:none;position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#7c1212;color:#fff;
  padding:10px 20px;border-radius:8px;font-size:12px;z-index:99}
@media (max-width:560px){header,.view,nav{padding-left:12px;padding-right:12px}.big{font-size:38px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<header>
  <div class="brand"><div class="dot" id="live-dot"></div>
    <div><b>SERVER MONITOR</b><small id="hdr-os">Linux Health Dashboard</small></div></div>
  <div class="meta">
    <span class="pill na" id="pill" role="status">…</span>
    <span>Host <b id="hdr-host">—</b></span><span>Up <b id="hdr-up" class="ok-c">—</b></span>
    <span>Kernel <b id="hdr-kernel" class="sky-c">—</b></span><b id="clock">—</b>
  </div>
</header>

<nav class="tabs" role="tablist" aria-label="Sections">
  <button class="tab" role="tab" data-tab="dashboard">📊 Dashboard</button>
  <button class="tab" role="tab" data-tab="processes">⚙ Processes</button>
  <button class="tab" role="tab" data-tab="network">🌐 Network</button>
  <button class="tab" role="tab" data-tab="service">🔍 Service Check</button>
  <button class="tab" role="tab" data-tab="thresholds">🎚 Thresholds</button>
  <button class="tab" role="tab" data-tab="reports">📄 Reports</button>
  <button class="tab" role="tab" data-tab="logs">📋 Logs</button>
</nav>

<section class="view" id="v-dashboard" role="tabpanel"><div class="grid">
  <div class="card"><h3>CPU</h3>
    <div class="big" id="cpu-pct">—</div><div class="sub" id="cpu-model">—</div>
    <div class="gauge"><div class="track"><div class="fill" id="cpu-bar"></div><div class="tick" id="cpu-tick"></div></div>
      <div class="glabel" id="cpu-cores">—</div></div>
    <canvas id="cpu-spark" aria-label="CPU history"></canvas>
    <div class="legend">last 3 minutes · dashed line = threshold</div>
    <div class="row" style="margin-top:6px"><span class="k">Load 1m</span><span class="v" id="la1">—</span></div>
    <div class="row"><span class="k">Load 5m</span><span class="v" id="la5">—</span></div>
    <div class="row"><span class="k">Load 15m</span><span class="v" id="la15">—</span></div></div>
  <div class="card"><h3>Memory (RAM)</h3>
    <div class="big" id="mem-pct">—</div><div class="sub">of RAM in use</div>
    <div class="gauge"><div class="track"><div class="fill" id="mem-bar"></div><div class="tick" id="mem-tick"></div></div>
      <div class="glabel" id="mem-lbl">—</div></div>
    <canvas id="mem-spark" aria-label="Memory history"></canvas>
    <div class="legend">last 3 minutes · dashed line = threshold</div>
    <div class="row" style="margin-top:6px"><span class="k">Total</span><span class="v sky-c" id="mem-total">—</span></div>
    <div class="row"><span class="k">Used</span><span class="v" id="mem-used">—</span></div>
    <div class="row"><span class="k">Available</span><span class="v ok-c" id="mem-free">—</span></div></div>
  <div class="card"><h3>Disk ( / )</h3>
    <div class="big" id="disk-pct">—</div><div class="sub" id="disk-fs">—</div>
    <div class="gauge"><div class="track"><div class="fill" id="disk-bar"></div><div class="tick" id="disk-tick"></div></div>
      <div class="glabel" id="disk-lbl">—</div></div>
    <div class="row" style="margin-top:14px"><span class="k">Total</span><span class="v" id="disk-total">—</span></div>
    <div class="row"><span class="k">Used</span><span class="v warn-c" id="disk-used">—</span></div>
    <div class="row"><span class="k">Free</span><span class="v ok-c" id="disk-free">—</span></div></div>
  <div class="card"><h3>Threshold alerts</h3><div id="alerts"></div>
    <div class="hint" id="thr-hint"></div></div>
  <div class="card"><h3>Logged-in users</h3><div id="users"></div></div>
  <div class="card"><h3>System info</h3>
    <div class="row"><span class="k">Hostname</span><span class="v" id="si-host">—</span></div>
    <div class="row"><span class="k">OS</span><span class="v" id="si-os">—</span></div>
    <div class="row"><span class="k">Kernel</span><span class="v sky-c" id="si-kernel">—</span></div>
    <div class="row"><span class="k">Uptime</span><span class="v ok-c" id="si-up">—</span></div>
    <div class="row"><span class="k">CPU</span><span class="v" id="si-cpu" style="max-width:190px">—</span></div>
    <div class="row"><span class="k">Cores</span><span class="v" id="si-cores">—</span></div></div>
  <div class="card wide"><h3>All filesystems</h3><div class="scroll"><table>
    <thead><tr><th>Filesystem</th><th>Size</th><th>Used</th><th>Free</th><th>Mount</th><th style="min-width:140px">Usage</th></tr></thead>
    <tbody id="disks"></tbody></table></div></div>
</div></section>

<section class="view" id="v-processes" role="tabpanel"><div class="card">
  <div class="title">Top 10 processes by CPU</div><div class="scroll"><table>
    <thead><tr><th>PID</th><th>User</th><th style="min-width:150px">CPU %</th><th>MEM %</th><th>Command</th></tr></thead>
    <tbody id="procs"></tbody></table></div>
  <div class="hint">CPU % is measured over the last few seconds (like <b>top</b>); 100 % = one full core.</div></div></section>

<section class="view" id="v-network" role="tabpanel">
  <div class="title">Network interfaces</div>
  <div id="net" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px"></div></section>

<section class="view" id="v-service" role="tabpanel"><div class="card" style="max-width:520px">
  <div class="title">Check a system service</div>
  <label class="f"><span>Service name</span><input type="text" id="svc-name" maxlength="128" placeholder="e.g. ssh, cron" autocomplete="off"></label>
  <button class="btn skyb" id="svc-btn">Check status</button>
  <div class="chips" id="chips"><button class="chip">ssh</button><button class="chip">cron</button><button class="chip">rsyslog</button><button class="chip">ufw</button><button class="chip">nginx</button></div>
  <div class="hint">Allowed: letters, digits and . _ - @ : (max 128 characters). Read-only: never starts or stops a service.</div>
  <div id="svc-out" style="margin-top:16px" aria-live="polite"></div></div></section>

<section class="view" id="v-thresholds" role="tabpanel"><div class="card" style="max-width:440px">
  <div class="title">Alert thresholds</div>
  <p style="color:var(--dim);font-size:12px;margin-bottom:16px">WARNING is shown when usage ≥ threshold. Whole numbers 1–100.</p>
  <label class="f"><span>CPU %</span><input type="number" id="th-cpu" min="1" max="100" step="1" inputmode="numeric"></label>
  <label class="f"><span>RAM %</span><input type="number" id="th-ram" min="1" max="100" step="1" inputmode="numeric"></label>
  <label class="f"><span>Disk %</span><input type="number" id="th-disk" min="1" max="100" step="1" inputmode="numeric"></label>
  <div style="display:flex;gap:10px"><button class="btn green" id="th-save">Save</button><button class="btn grey" id="th-reset">Reset to 80</button></div>
  <div class="msg" id="th-msg" aria-live="polite"></div></div></section>

<section class="view" id="v-reports" role="tabpanel"><div class="card" style="max-width:640px">
  <div class="title">Generate &amp; download reports</div>
  <button class="btn green" id="rp-gen">Generate new report</button>
  <div class="msg" id="rp-msg" aria-live="polite"></div>
  <div style="margin:20px 0 10px;font-size:11px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase">Saved reports (newest first)</div>
  <div id="rp-list"></div><pre class="box" id="rp-view" style="display:none;margin-top:12px"></pre></div></section>

<section class="view" id="v-logs" role="tabpanel"><div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
    <div class="title" style="margin:0">Activity log (last 30 entries)</div><button class="btn grey" id="lg-refresh">↻ Refresh</button></div>
  <pre class="box" id="lg-box">Loading…</pre></div></section>

<div id="offline" role="alert">⚠ Cannot reach the server. Retrying…</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MAXPTS=60;                       // 60 samples x 3 s = 3 minutes of history
const HX={cpu:[],mem:[]};
let THR={cpu:80,ram:80,disk:80}, timer=null, busy=false, fails=0, logTimer=null;

/* ---- API helper: custom header (CSRF guard), JSON, timeout, readable errors ---- */
async function api(path,opts={}){
  const ctl=new AbortController(), to=setTimeout(()=>ctl.abort(),8000);
  try{
    const r=await fetch(path,{...opts,signal:ctl.signal,headers:{'X-Requested-With':'monitor-ui','Content-Type':'application/json'}});
    let d=null; try{d=await r.json();}catch(e){}
    if(!r.ok) throw new Error((d&&d.error)||('HTTP '+r.status));
    return d;
  } finally { clearTimeout(to); }
}
const post=(p,body)=>api(p,{method:'POST',body:JSON.stringify(body||{})});

/* ---- helpers ---- */
const fmtMB=mb=>mb==null?'—':mb>=1024?(mb/1024).toFixed(1)+' GB':Math.round(mb)+' MB';
const fmtRate=k=>k>=1024?(k/1024).toFixed(1)+' MB/s':k.toFixed(1)+' KB/s';
const pct=v=>v==null?'—':v+'%';
function colorOf(v,t,st){ if(st==='UNAVAILABLE'||v==null) return 'var(--muted)'; return v>=t?'var(--danger)':v>=t*.75?'var(--warn)':'var(--accent)'; }
function setGauge(id,v,t,st){ const c=colorOf(v,t,st), f=$(id+'-bar'); f.style.width=Math.min(v||0,100)+'%'; f.style.background=c;
  $(id+'-tick').style.left=Math.min(t,100)+'%'; $(id+'-pct').style.color=c; $(id+'-pct').textContent=pct(v); }

function spark(id,data,rgb,thr){
  const c=$(id), W=c.clientWidth||300, H=46, dpr=window.devicePixelRatio||1;
  c.width=W*dpr; c.height=H*dpr; const x=c.getContext('2d'); x.scale(dpr,dpr); x.clearRect(0,0,W,H);
  const y=v=>H-2-(Math.min(v,100)/100)*(H-4), step=W/(MAXPTS-1);   // fixed 0-100 % scale: no misleading zoom
  x.setLineDash([4,4]); x.strokeStyle='rgba(226,232,240,.35)'; x.lineWidth=1; x.beginPath(); x.moveTo(0,y(thr)); x.lineTo(W,y(thr)); x.stroke(); x.setLineDash([]);
  if(data.length<2) return;
  const x0=W-(data.length-1)*step;                                    // newest sample sits at the right edge
  x.beginPath(); data.forEach((v,i)=>i?x.lineTo(x0+i*step,y(v)):x.moveTo(x0,y(v)));
  x.strokeStyle='rgb('+rgb+')'; x.lineWidth=1.6; x.stroke();
  x.lineTo(W,H); x.lineTo(x0,H); x.closePath();
  const g=x.createLinearGradient(0,0,0,H); g.addColorStop(0,'rgba('+rgb+',.28)'); g.addColorStop(1,'rgba('+rgb+',0)'); x.fillStyle=g; x.fill();
}
const push=(a,v)=>{ if(v!=null){a.push(v); if(a.length>MAXPTS)a.shift();} };

/* ---- render the metrics payload ---- */
function render(d){
  THR=d.thresholds; const A=d.alerts;
  $('hdr-host').textContent=d.os.host; $('hdr-os').textContent=d.os.name; $('hdr-up').textContent=d.uptime; $('hdr-kernel').textContent=d.os.kernel;
  const p=$('pill'), na=Object.values(A).includes('UNAVAILABLE');
  p.className='pill '+(d.warnings?'bad':na?'na':'ok'); p.textContent=d.warnings?('⚠ '+d.warnings+(d.warnings>1?' WARNINGS':' WARNING')):na?'PARTIAL DATA':'✓ ALL OK';

  setGauge('cpu',d.cpu.percent,THR.cpu,A.cpu); $('cpu-model').textContent=d.cpu.model;
  $('cpu-cores').textContent=d.cpu.cores+(d.cpu.cores===1?' core':' cores');
  $('la1').textContent=d.load.m1||'—'; $('la5').textContent=d.load.m5||'—'; $('la15').textContent=d.load.m15||'—';
  push(HX.cpu,d.cpu.percent); spark('cpu-spark',HX.cpu,'16,185,129',THR.cpu);

  const m=d.mem; setGauge('mem',m.percent,THR.ram,A.ram); $('mem-lbl').textContent=fmtMB(m.total_mb);
  $('mem-total').textContent=fmtMB(m.total_mb); $('mem-used').textContent=fmtMB(m.used_mb); $('mem-free').textContent=fmtMB(m.free_mb);
  push(HX.mem,m.percent); spark('mem-spark',HX.mem,'56,189,248',THR.ram);

  const k=d.disk; setGauge('disk',k.percent,THR.disk,A.disk); $('disk-fs').textContent=k.fs+' → '+k.mount;
  $('disk-lbl').textContent=k.total_gb==null?'—':k.total_gb+' GB';
  $('disk-total').textContent=k.total_gb==null?'—':k.total_gb+' GB'; $('disk-used').textContent=k.used_gb==null?'—':k.used_gb+' GB'; $('disk-free').textContent=k.free_gb==null?'—':k.free_gb+' GB';

  const row=(n,v,t,s)=>{const c=s==='OK'?'ok':s==='WARNING'?'bad':'na', col=s==='OK'?'ok-c':s==='WARNING'?'bad-c':'warn-c';
    return `<div class="alert ${c}"><span class="${col}" style="font-size:15px">${s==='OK'?'✓':'⚠'}</span><span style="flex:1">${n}</span><b class="${col}">${pct(v)} — ${s}</b></div>`;};
  $('alerts').innerHTML=row('CPU',d.cpu.percent,THR.cpu,A.cpu)+row('RAM',m.percent,THR.ram,A.ram)+row('Disk',k.percent,THR.disk,A.disk);
  $('thr-hint').textContent=`Thresholds: CPU ${THR.cpu}% · RAM ${THR.ram}% · Disk ${THR.disk}%`;

  $('users').innerHTML=d.users.length?d.users.map(u=>`<div class="row"><b class="ok-c">${esc(u.user)}</b><span class="k">${esc(u.tty)} · ${esc(u.from)}</span></div>`).join(''):'<div class="mut-c">No users logged in.</div>';
  $('si-host').textContent=d.os.host; $('si-os').textContent=d.os.name; $('si-kernel').textContent=d.os.kernel; $('si-up').textContent=d.uptime;
  $('si-cpu').textContent=d.cpu.model; $('si-cpu').title=d.cpu.model; $('si-cores').textContent=d.cpu.cores;

  $('disks').innerHTML=d.disks.map(x=>{const q=parseInt(x.pct)||0,c=q>=THR.disk?'var(--danger)':q>=THR.disk*.75?'var(--warn)':'var(--accent)';
    return `<tr><td class="k">${esc(x.fs)}</td><td>${esc(x.size)}</td><td>${esc(x.used)}</td><td class="ok-c">${esc(x.avail)}</td><td class="sky-c">${esc(x.mount)}</td>
      <td><div class="bar"><div class="track"><div class="fill" style="width:${Math.min(q,100)}%;background:${c}"></div></div><span style="color:${c}">${esc(x.pct)}</span></div></td></tr>`;}).join('');

  $('procs').innerHTML=d.procs.length?d.procs.map(x=>{const c=x.cpu>=50?'var(--danger)':x.cpu>=20?'var(--warn)':'var(--accent)';
    return `<tr><td class="mut-c">${x.pid}</td><td>${esc(x.user)}</td>
      <td><div class="bar"><div class="track"><div class="fill" style="width:${Math.min(x.cpu,100)}%;background:${c}"></div></div><span style="color:${c}">${x.cpu}%</span></div></td>
      <td class="k">${x.mem}%</td><td class="sky-c">${esc(x.cmd)}</td></tr>`;}).join(''):'<tr><td colspan="5" class="mut-c">Collecting data…</td></tr>';

  $('net').innerHTML=d.net.length?d.net.map(n=>`<div class="card" style="background:var(--panel2)"><div style="font-weight:700;margin-bottom:8px">${esc(n.name)}</div>
    <div class="row"><span class="k">↓ Download</span><span class="v sky-c">${fmtRate(n.rx_kbs)}</span></div>
    <div class="row"><span class="k">↑ Upload</span><span class="v ok-c">${fmtRate(n.tx_kbs)}</span></div>
    <div class="row"><span class="k">Received total</span><span class="v">${n.rx_mb} MB</span></div>
    <div class="row"><span class="k">Sent total</span><span class="v">${n.tx_mb} MB</span></div></div>`).join(''):'<div class="mut-c">No network interfaces found.</div>';
}

/* ---- polling: one request at a time, paused while the tab is hidden, back-off on errors ---- */
function setConn(ok){ $('offline').style.display=ok?'none':'block'; $('live-dot').classList.toggle('off',!ok); }
async function poll(){
  if(busy) return; busy=true;
  try{ render(await api('/api/metrics')); fails=0; setConn(true); }
  catch(e){ fails++; setConn(false); console.error(e); }
  finally{ busy=false; clearTimeout(timer); timer=setTimeout(()=>{ if(!document.hidden) poll(); else schedule(); }, Math.min(3000*Math.pow(2,fails),15000)); }
}
function schedule(){ clearTimeout(timer); timer=setTimeout(()=>{ if(!document.hidden) poll(); else schedule(); },3000); }
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) poll(); });

/* ---- tabs ---- */
function showTab(name){
  if(!$('v-'+name)) name='dashboard';
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id==='v-'+name));
  document.querySelectorAll('.tab').forEach(t=>t.setAttribute('aria-selected',t.dataset.tab===name));
  history.replaceState(null,'','#'+name);
  clearInterval(logTimer);
  if(name==='reports') loadReports();
  if(name==='logs'){ loadLogs(); logTimer=setInterval(loadLogs,5000); }
  if(name==='thresholds') loadThresholds();
  if(name==='dashboard') setTimeout(()=>{HX.cpu.length&&spark('cpu-spark',HX.cpu,'16,185,129',THR.cpu);HX.mem.length&&spark('mem-spark',HX.mem,'56,189,248',THR.ram);},0);
}
document.querySelector('.tabs').addEventListener('click',e=>{ const b=e.target.closest('.tab'); if(b) showTab(b.dataset.tab); });
window.addEventListener('hashchange',()=>showTab(location.hash.slice(1)||'dashboard'));   // back/forward and typed #tab

/* ---- service check ---- */
const showMsg=(el,cls,text)=>{ el.className='msg '+cls; el.textContent=text; };
async function checkService(){
  const name=$('svc-name').value.trim(), out=$('svc-out'), btn=$('svc-btn');
  if(!name){ out.innerHTML='<div class="bad-c">Enter a service name.</div>'; return; }
  btn.disabled=true; out.innerHTML='<div class="k">Checking…</div>';
  try{
    const d=await post('/api/service',{name});
    if(d.error){ out.innerHTML=`<div class="bad-c">⚠ ${esc(d.error)}</div>`; return; }
    const cls=s=>s==='active'||s==='enabled'?'ok':(s==='inactive'||s==='disabled'||s==='failed')?'bad':'na';
    out.innerHTML=`<div style="border:1px solid var(--border);border-radius:8px;padding:14px;background:var(--bg)">
      <div style="font-size:14px;font-weight:700;margin-bottom:6px">${esc(d.name)}</div>
      ${d.description?`<div class="k" style="font-size:11px;margin-bottom:10px">${esc(d.description)}</div>`:''}
      <div class="row"><span class="k">Active state</span><span class="badge ${cls(d.active)}">${esc(d.active)}</span></div>
      <div class="row"><span class="k">Sub-state</span><span class="k">${esc(d.sub||'—')}</span></div>
      <div class="row"><span class="k">Start at boot</span><span class="badge ${cls(d.enabled)}">${esc(d.enabled)}</span></div>
      <div class="hint">ⓘ This tool only reads status. It never starts or stops services.</div></div>`;
  }catch(e){ out.innerHTML=`<div class="bad-c">⚠ ${esc(e.message)}</div>`; }
  finally{ btn.disabled=false; }
}
$('svc-btn').addEventListener('click',checkService);
$('svc-name').addEventListener('keydown',e=>{ if(e.key==='Enter') checkService(); });
$('chips').addEventListener('click',e=>{ if(e.target.classList.contains('chip')){ $('svc-name').value=e.target.textContent; checkService(); } });

/* ---- thresholds ---- */
async function loadThresholds(){ try{ const t=await api('/api/thresholds'); ['cpu','ram','disk'].forEach(k=>$('th-'+k).value=t[k]); THR=t; }catch(e){ showMsg($('th-msg'),'bad',e.message); } }
async function saveThresholds(vals){
  const body={}, msg=$('th-msg');
  for(const k of ['cpu','ram','disk']){
    const v=String(vals?vals[k]:$('th-'+k).value).trim();
    if(!/^[1-9][0-9]{0,2}$/.test(v)||+v>100){ showMsg(msg,'bad',`⚠ ${k.toUpperCase()}: enter a whole number from 1 to 100`); return; }
    body[k]=+v;
  }
  try{ const d=await post('/api/thresholds',body); THR=d.thresholds; ['cpu','ram','disk'].forEach(k=>$('th-'+k).value=THR[k]);
       showMsg(msg,'ok','✓ Thresholds saved'); poll(); setTimeout(()=>{msg.className='msg';},3500); }
  catch(e){ showMsg(msg,'bad','⚠ '+e.message); }
}
$('th-save').addEventListener('click',()=>saveThresholds());
$('th-reset').addEventListener('click',()=>saveThresholds({cpu:80,ram:80,disk:80}));
['th-cpu','th-ram','th-disk'].forEach(id=>$(id).addEventListener('keydown',e=>{ if(e.key==='Enter') saveThresholds(); }));

/* ---- reports ---- */
async function loadReports(){
  try{
    const list=await api('/api/reports'), box=$('rp-list');
    box.innerHTML=list.length?list.map(f=>`<div class="item"><div><div class="n">📄 ${esc(f.name)}</div><div class="s">${esc(f.mtime)} · ${(f.size/1024).toFixed(1)} KB</div></div>
      <div style="display:flex;gap:6px"><button class="btn grey" style="padding:5px 12px;font-size:11px" data-view="${esc(f.name)}">View</button>
      <a href="/api/reports/${encodeURIComponent(f.name)}" download><button class="btn skyb" style="padding:5px 12px;font-size:11px">Download</button></a></div></div>`).join('')
      :'<div class="mut-c">No reports yet. Click “Generate new report”.</div>';
  }catch(e){ showMsg($('rp-msg'),'bad','⚠ '+e.message); }
}
$('rp-list').addEventListener('click',async e=>{
  const n=e.target.dataset&&e.target.dataset.view; if(!n) return;
  try{ const r=await fetch('/api/reports/'+encodeURIComponent(n)+'?view=1'); const v=$('rp-view'); v.textContent=await r.text(); v.style.display='block'; v.scrollIntoView({block:'nearest'}); }
  catch(err){ showMsg($('rp-msg'),'bad','⚠ '+err.message); }
});
$('rp-gen').addEventListener('click',async()=>{
  const b=$('rp-gen'); b.disabled=true;
  try{ const d=await post('/api/report'); showMsg($('rp-msg'),'ok','✓ Report saved: '+d.filename); loadReports(); setTimeout(()=>{$('rp-msg').className='msg';},4000); }
  catch(e){ showMsg($('rp-msg'),'bad','⚠ '+e.message); }
  finally{ b.disabled=false; }
});

/* ---- logs ---- */
async function loadLogs(){
  try{ const lines=await api('/api/logs'), box=$('lg-box');
    box.innerHTML=lines.length?lines.map(l=>`<span class="${l.includes('[ERROR]')?'ln-e':l.includes('[WARN]')?'ln-w':'ln-i'}">${esc(l)}</span>`).join('\n'):'<span class="mut-c">No log entries yet.</span>';
    box.scrollTop=box.scrollHeight;
  }catch(e){ $('lg-box').textContent='⚠ '+e.message; }
}
$('lg-refresh').addEventListener('click',loadLogs);

/* ---- clock + start ---- */
(function tick(){ const d=new Date(),p=n=>String(n).padStart(2,'0');
  $('clock').textContent=`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; setTimeout(tick,1000); })();
showTab(location.hash.slice(1)||'dashboard');
poll();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Linux Server Health Monitor - web dashboard")
    ap.add_argument("--host", default=os.environ.get("MONITOR_HOST", "127.0.0.1"),
                    help="address to listen on (default 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MONITOR_PORT", "5000")),
                    help="port (default 5000)")
    args = ap.parse_args()
    if not 1 <= args.port <= 65535:
        ap.error("port must be 1-65535")
    print("=" * 56)
    print(f"  Linux Server Monitor - Web Dashboard v{VERSION}")
    print(f"  Open : http://{'localhost' if args.host in ('127.0.0.1', '::1') else args.host}:{args.port}")
    print(f"  Data : {DATA_DIR}")
    print("  Stop : Ctrl+C")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: listening on the network. The dashboard has no login -")
        print("           use this only on a trusted network.")
    print("=" * 56)
    log("INFO", f"Web dashboard started on {args.host}:{args.port} (v{VERSION})")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
