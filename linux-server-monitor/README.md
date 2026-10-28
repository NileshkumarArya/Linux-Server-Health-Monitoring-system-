# Linux Server Health Monitoring & Alert System  (v1.1)

Two ways to watch the health of an Ubuntu/Linux machine — CPU, memory, disk, uptime, users, processes and services — with `OK` / `WARNING` alerts, saved reports and an activity log:

| | Terminal tool | Web dashboard |
|---|---|---|
| File | `server_monitor.sh` (Bash) | `web/app.py` (Python + Flask) |
| Start | `bash server_monitor.sh` | `python3 web/app.py` → http://localhost:5000 |
| Needs | Bash 4+ (preinstalled) | Python 3 + `pip3 install flask` |

Both are **read-only** (no process is killed, no service is restarted), need **no sudo**, and share one data folder, so a threshold changed in one is used by the other.

---

## 1. Quick start (Ubuntu)

```bash
unzip linux-server-monitor.zip && cd linux-server-monitor

# Terminal tool
bash server_monitor.sh

# Web dashboard
pip3 install -r requirements.txt        # once
python3 web/app.py                      # then open http://localhost:5000
```

If the folder came from Windows and Bash complains about `\r`: `sed -i 's/\r$//' server_monitor.sh`

## 2. Project layout

```
linux-server-monitor/
├── server_monitor.sh     Bash terminal tool (menu + --check / --report modes)
├── web/
│   ├── app.py            Flask backend + embedded HTML/CSS/JS dashboard
│   └── test_app.py       28 automated tests for the web backend
├── requirements.txt      flask
├── README.md             this file
├── TEST_CHECKLIST.md     manual test sheet (fill in on your VM)
├── docs/                 original requirement documents
└── data/                 created on first run (shared by both tools)
    ├── config.conf       CPU_THRESHOLD / RAM_THRESHOLD / DISK_THRESHOLD
    ├── logs/app.log      timestamped activity log (rotates at 1 MiB)
    └── reports/          report_YYYYMMDD_HHMMSS.txt
```

## 3. Terminal tool

Menu: 1 summary · 2 CPU · 3 memory · 4 disk · 5 uptime, users and top processes · 6 service check · 7 thresholds · 8 alerts · 9 report · 10 logs/reports · 11 exit.

Non-interactive options (for cron):

```bash
bash server_monitor.sh --check     # prints the alert table; exit 0 = all OK, 1 = a WARNING
bash server_monitor.sh --report    # saves one report;       exit 0 = saved,  1 = failed
bash server_monitor.sh --help | --version
```

Cron example (`crontab -e`):

```
*/5  * * * * bash ~/linux-server-monitor/server_monitor.sh --check  >/dev/null 2>&1
*/15 * * * * bash ~/linux-server-monitor/server_monitor.sh --report >/dev/null 2>&1
```

How values are measured: **CPU** = two `/proc/stat` samples 1 s apart (non-idle ÷ total); **RAM** = `MemTotal − MemAvailable` from `/proc/meminfo`; **Disk** = df's own `Use%` for `/`. **Alert rule:** `WARNING` when usage ≥ threshold, `UNAVAILABLE` when a value cannot be read.

## 4. Web dashboard

Seven tabs: **Dashboard** (CPU/RAM/disk gauges with threshold markers, 3-minute history, alerts, users, filesystems) · **Processes** (top 10, live CPU %) · **Network** (speed and totals) · **Service Check** · **Thresholds** · **Reports** (generate, view, download) · **Logs**. A status badge in the header shows `✓ ALL OK` or the number of warnings. The page refreshes every 3 s, pauses while the browser tab is hidden, and shows a banner (then retries) if the server stops answering.

```bash
python3 web/app.py                       # this machine only (127.0.0.1) - default
python3 web/app.py --host 0.0.0.0        # reachable from other computers, e.g. VirtualBox bridged mode
python3 web/app.py --port 8080
python3 -m unittest -v test_app          # run from inside web/ (28 tests)
```

> The dashboard has **no login**. Use `--host 0.0.0.0` only on a network you trust.

VirtualBox: set the adapter to *Bridged*, find the VM address with `hostname -I`, start with `--host 0.0.0.0`, then open `http://<vm-ip>:5000` from Windows.

Environment variables: `SERVER_MONITOR_DATA_DIR` (use another data folder, both tools), `MONITOR_HOST`, `MONITOR_PORT`, `NO_COLOR` (terminal tool).

## 5. What changed in v1.1 (bugs fixed and optimisations)

**Bugs fixed**

| Problem in v1.0 | Fix |
|---|---|
| Requesting a bad report file name crashed the server (`abort` was never imported) | Strict name check, clean 400/404 JSON reply |
| Sparkline code built its gradient from a malformed colour string (it only worked because browsers repair it), and the chart rescaled itself to its own maximum, so a flat 5 % line looked like 100 % | Clean colour handling; fixed 0–100 % scale with a dashed threshold line |
| A stopped service showed `unknown` instead of `inactive` (non-zero exit code of `systemctl is-active` was treated as "no data") | One `systemctl show` call; state read from its output |
| A 129-character service name was silently cut to 128 characters and accepted | No truncation; rejected as invalid |
| Process names with spaces (`tmux: server`) were cut to the first word | Read from `/proc/<pid>/stat` |
| "1 cores" | "1 core" |
| Process CPU % was the lifetime average from `ps` | Measured over the last seconds (like `top`) in the web app; the terminal tool now says clearly that `ps` gives an average |
| Disk errors showed `0%` (looked like a real value) | Shown as `UNAVAILABLE` |
| Threshold file written without an atomic replace (web) | Temp file + atomic replace, same as the Bash tool |

**Optimisations**

* `/api/metrics` answers in about **4 ms** instead of **513 ms** — CPU, network and per-process figures are sampled once per second by a background thread instead of sleeping inside every request.
* Static facts (OS, kernel, CPU model) are read once; `df`/`who` results are cached for a few seconds; the config file is re-read only when it changes; the log is tailed instead of read completely.
* The page polls with a single request at a time, backs off on errors and stops polling while hidden.

**Security**

* Listens on `127.0.0.1` by default (was `0.0.0.0`).
* Every POST must carry a custom header (blocks cross-site requests); security headers and a Content-Security-Policy are sent; everything shown from the system is HTML-escaped.
* Report downloads accept only `report_YYYYMMDD_HHMMSS.txt` names; thresholds must be whole numbers 1–100 (all-or-nothing update).
* Log lines cannot be forged with line breaks; logs rotate at 1 MiB.

## 6. Limitations

* Snapshots only — no history database, e-mail or SMS alerts; the dashboard keeps the last 3 minutes in the browser.
* CPU % in the terminal tool is a one-second sample, so it varies from run to run.
* Service check needs systemd (it says so in containers / WSL1).
* The disk alert uses `/` only; other filesystems are listed for information.
* Only the local machine is monitored. Tested on Ubuntu 24.04; other distributions are not guaranteed.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: flask` | `pip3 install flask` |
| `Address already in use` | another program uses port 5000: `python3 web/app.py --port 5001` |
| Cannot open the dashboard from Windows | start with `--host 0.0.0.0` and use the VM's IP address |
| `Permission denied` on `./server_monitor.sh` | `bash server_monitor.sh` or `chmod +x server_monitor.sh` |
| "Cannot create or write to the data directory" | check you own the folder (`ls -ld .`) and the disk is not full |
