# Test Checklist — Linux Server Health Monitoring & Alert System

**Tester name:** ______________________  **Date:** ______________  **Ubuntu version:** ______________  **Bash version:** ______________

## How to use this sheet

1. Run each test on **your Ubuntu machine / VirtualBox VM** from inside the project folder (`bash ./server_monitor.sh`).
2. Write what really happened in **Actual result (your VM)** and tick **Pass** or **Fail**.
3. Take a screenshot for each test and save it as `evidence/<Test ID>.png` (these go into the project report).

**"Pre-check" column** = what was already tried by the developer in an Ubuntu 24.04 Linux sandbox (no VirtualBox, no real systemd, no interactive terminal) while building the project:

* **Done** — executed in the sandbox with the expected outcome observed.
* **Partial** — only part of the test could be run (see the note).
* **Not run** — could not be run there; **you must run it on your VM.**

A "Done" pre-check does **not** replace your own run on the VM — it only means the test was not blind.

---

## A. Static check

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| ST-01 | Shell syntax | `bash -n server_monitor.sh` | No output, exit code 0 (`echo $?` prints `0`) | Done | | |
| ST-02 | No unsafe constructs | `grep -nE '\beval\b\|\bsudo\b' server_monitor.sh` | Matches appear only in comments, never in executable code | Done | | |

## B. Menu behaviour

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| M-01 | Start the program | `bash ./server_monitor.sh` | Menu with 11 numbered options is shown; `data/` folder is created | Done | | |
| M-02 | Valid choice returns to menu | Choose `2`, press Enter | CPU details shown, then the menu appears again | Partial (Enter-pause needs a real terminal) | | |
| M-03 | Text instead of number | Type `abc` | Error "Invalid choice 'abc'…", menu shown again, no crash | Done | | |
| M-04 | Number out of range | Type `0`, then `12`, then `99` | Each rejected with an error, menu shown again | Done (`0`, `99`) | | |
| M-05 | Empty input | Press Enter on an empty line | Error "You did not enter anything…", menu shown again | Done | | |
| M-06 | Exit | Choose `11` | Goodbye message, program ends, `echo $?` prints `0` | Done | | |
| M-07 | Ctrl+D (end of input) | Press Ctrl+D at the menu | Program exits cleanly with goodbye message (no endless loop) | Done (closed input) | | |
| M-08 | Ctrl+C | Press Ctrl+C at the menu | Clean exit with goodbye message; log shows "Interrupted by user" | Done | | |
| M-09 | `--help` / `--version` / bad option | `bash ./server_monitor.sh --help`, `--version`, `--xyz` | Usage text; version line; error + usage and exit code 2 for `--xyz` | Done | | |

## C. Metric readings (compare with direct Linux commands in a second terminal)

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| ME-01 | CPU reading | Menu `2`; compare with `top` (idle `id` value → usage ≈ 100 − id) | Value is close to `top` (small differences are normal — different sampling window) | Partial (value shown; not compared with `top`) | | |
| ME-02 | CPU under load | Terminal B: `yes > /dev/null`; Terminal A: menu `2` | CPU % clearly higher (about 50–100 % depending on core count); stop `yes` with Ctrl+C | Not run | | |
| ME-03 | Memory reading | Menu `3`; compare with `free -m` | Total/used/available match `free -m` within a few MiB; usage % ≈ used ÷ total | Done (185 MiB used = `free -m`) | | |
| ME-04 | Disk reading | Menu `4`; compare with `df -h /` | Filesystem, mount point and Use% identical to `df -h /` | Done (47 % = `df`) | | |
| ME-05 | Uptime / load | Menu `5`; compare with `uptime` | Uptime and three load averages agree with `uptime` | Done (shown; compared by eye) | | |
| ME-06 | Logged-in users | Menu `5`; compare with `who` | Same users as `who`; if none, message "No users are currently logged in." | Partial (empty case only) | | |
| ME-07 | Process snapshot | Menu `5`; compare with `ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu \| head` | Up to 10 rows, highest CPU first | Done | | |
| ME-08 | Summary screen | Menu `1` | OS, kernel, uptime, load, and the CPU/RAM/Disk table are all shown | Done | | |

## D. Threshold configuration (menu 7)

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| TH-01 | Lowest valid value | `7` → `1` → enter `1` | "CPU threshold set to 1%." | Done | | |
| TH-02 | Highest valid value | `7` → `1` → enter `100` | "CPU threshold set to 100%." | Done | | |
| TH-03 | Zero | enter `0` | Rejected; old value unchanged | Done | | |
| TH-04 | Above range | enter `101` | Rejected; old value unchanged | Done | | |
| TH-05 | Negative | enter `-5` | Rejected; old value unchanged | Done | | |
| TH-06 | Decimal | enter `5.5` | Rejected; old value unchanged | Done | | |
| TH-07 | Text | enter `abc` | Rejected; old value unchanged | Done | | |
| TH-08 | Empty | press Enter | Rejected; old value unchanged | Done | | |
| TH-09 | RAM and Disk thresholds | Change RAM (option 2) and Disk (option 3) to `70` | Both accepted; shown in the menu header | Partial (CPU tested most; RAM/Disk set to 1 and 100) | | |
| TH-10 | Persistence | Set CPU = `65`, exit (`11`), start again | Menu header shows `CPU 65%`; `cat data/config.conf` shows `CPU_THRESHOLD=65` | Partial (loading from file verified; not a full set-exit-restart cycle) | | |
| TH-11 | Reset to defaults | `7` → `4` | All three become 80 | Not run | | |

## E. Alerts (menu 8)

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| AL-01 | Trigger WARNING | Set CPU, RAM and Disk thresholds to `1`, then menu `8` | All three rows show `WARNING` (red on a colour terminal) | Done | | |
| AL-02 | Show OK | Set all three to `100`, then menu `8` | All rows show `OK` (green) unless usage is really 100 % | Done | | |
| AL-03 | Boundary (≥ rule) | Note the Disk % from menu `4` (e.g. 47 %), set Disk threshold to exactly that number, menu `8` | Disk row shows `WARNING` (equal counts as warning) | Partial (CPU 1.0 % vs 1 % gave WARNING) | | |
| AL-04 | Alerts are logged | After AL-01 open `data/logs/app.log` | Lines like `[WARN] ALERT: CPU usage … >= threshold 1%` | Done | | |

## F. Service check (menu 6) — needs systemd

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| SV-01 | Running service | Menu `6` → `ssh` (or `cron`) | Active state `active`, start-at-boot shown; compare with `systemctl is-active ssh` | **Not run** (no systemd in sandbox; only a stub `systemctl` was used to test the parsing) | | |
| SV-02 | Stopped/inactive service | Use any installed but stopped service | Active state `inactive`; note that the tool does not start it | Not run | | |
| SV-03 | Service does not exist | Menu `6` → `nosuchservice` | "Service 'nosuchservice' was not found" | Partial (stub only) | | |
| SV-04 | Invalid names | Menu `6` → `bad name;ls`, `-x`, `$(id)` | Each rejected with "Invalid service name…"; nothing is executed | Done (with stub) | | |
| SV-05 | Empty name | Menu `6` → press Enter | "Service name cannot be empty." | Done (with stub) | | |
| SV-06 | systemd unavailable | Run where systemd is not running (container / WSL1), menu `6` | Clear "unavailable" warning, back to menu | Done | | |

## G. Reports and logs (menus 9 and 10)

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| RP-01 | Report created | Menu `9` | "Report saved: …/data/reports/report_YYYYMMDD_HHMMSS.txt" | Done | | |
| RP-02 | Report contents | `cat` the report file | Contains SYSTEM, CPU, MEMORY, DISK, ALL FILESYSTEMS, UPTIME, USERS, PROCESSES and ALERTS sections; no colour codes | Done | | |
| RP-03 | No overwrite | Create 2–3 reports quickly | Different file names each time (`_1`, `_2` added if the same second) | Done | | |
| RP-04 | Report list | Menu `10` → `2` | Newest report listed first (max 10) | Done | | |
| RP-05 | Open a report | Menu `10` → `3` → `1` | Report text displayed | Done | | |
| RP-06 | No reports yet | Delete `data/reports/*`, menu `10` → `2` | "No reports saved yet." | Partial (message path reviewed; deleted-folder case not run) | | |
| RP-07 | Log content and append | Menu `10` → `1`; run the program twice and `cat data/logs/app.log` | Each line has timestamp + level + message; earlier lines are kept (appended, not replaced) | Done | | |
| RP-08 | File permissions | `ls -l data/config.conf data/logs/app.log data/reports/` | Files are `-rw-------` (owner only) | Done | | |

## H. Failure handling

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| FL-01 | Missing command | Run with a restricted PATH that hides `who`/`df` (see note below) | Start-up warning lists missing utilities; those features say "unavailable"; program continues | Done | | |
| FL-02 | Unwritable data folder | `SERVER_MONITOR_DATA_DIR=/proc/nope bash ./server_monitor.sh`, then menus `7`, `9`, `10` | Clear error with path and hint; threshold NOT changed; monitoring options still work | Done | | |
| FL-03 | Corrupt config file | Put `CPU_THRESHOLD=abc` and `RAM_THRESHOLD=150` in `data/config.conf`, start the program | Warning; defaults used for bad values; file repaired | Done | | |
| FL-04 | Malicious config file | Put `CPU_THRESHOLD=$(touch /tmp/pwned)` in `data/config.conf`, start the program | Line ignored; `/tmp/pwned` is **not** created | Done | | |
| FL-05 | Read-only project folder | `chmod -w .` on the project folder (remove old `data/`), start | Warning that `~/.linux_server_monitor/` is used instead | Not run | | |
| FL-06 | Log becomes unwritable | Start the program, in another terminal `chmod 000 data/logs/app.log`, use a few menu items | One warning about logging; program keeps running | Not run | | |
| FL-07 | Path with spaces / other folder | Copy the folder to `~/my project/`, run it from `/` with the full path | Works; `data/` is created next to the script | Done | | |

*Note for FL-01:* `mkdir /tmp/fakebin; for c in bash cat sed head tail sleep date mkdir mv rm uname id nproc dirname; do ln -s "$(command -v $c)" /tmp/fakebin/$c; done; PATH=/tmp/fakebin bash ./server_monitor.sh`

## I. Safety and environment

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| SF-01 | Works without sudo | Run as a normal user (not root, no `sudo`) | All menu options work (service check may show limited details) | Partial (sandbox user is root) | | |
| SF-02 | Writes only inside data folder | Compare `find ~ -newer server_monitor.sh -type f` before/after a session | Only files under `data/` are new | Done | | |
| SF-03 | No automatic changes | Run all menu options | No service restarted, no process killed, no system file changed | Done (by code review and runs) | | |
| EN-01 | Ubuntu in VirtualBox | Run the whole checklist inside your VM | Everything above works on the VM | **Not run** | | |
| EN-02 | Terminal colours / screen clear | Run in a normal terminal window | Coloured OK/WARNING words; screen clears before each menu; "Press Enter" pause works | **Not run** (sandbox has no interactive terminal) | | |

---

## J. Web dashboard and automated tests (v1.1)

| ID | Test | Steps | Expected result | Pre-check | Actual result (your VM) | Pass / Fail |
|---|---|---|---|---|---|---|
| J-01 | Unit tests | `cd web && python3 -m unittest -v test_app` | `Ran 28 tests ... OK` | Done (28 passed) | | |
| J-02 | Dashboard loads | `python3 web/app.py`, open http://localhost:5000 | CPU/RAM/Disk cards filled, header badge shows `ALL OK`, no error banner | Done | | |
| J-03 | Tabs and deep link | Click every tab; open `http://localhost:5000/#thresholds` | Each tab opens; the link opens Thresholds | Done | | |
| J-04 | Thresholds form | Enter `0`, `101`, `5.5`, empty; then `70/75/40` and Save | Bad values rejected with a message; valid ones saved; `cat data/config.conf` shows them | Done | | |
| J-05 | Shared configuration | Save thresholds in the browser, then run `bash server_monitor.sh --check` | Terminal tool uses the same thresholds | Done | | |
| J-06 | Alert on dashboard | Set Disk threshold below the current disk % | Disk card turns red, badge shows `1 WARNING` | Done | | |
| J-07 | Service check - invalid | Service tab: `bad name;rm -rf /`, `-x`, 129 letters | Each shows "Invalid service name"; nothing runs | Done | | |
| J-08 | Service check - real | Service tab: `ssh`, `cron`, then a stopped service | `active`/`enabled` for running ones; `inactive` (not `unknown`) for a stopped one | **Not run** (no systemd in sandbox; stopped-service logic tested with simulated output) | | |
| J-09 | Reports | Reports tab: Generate twice, View, Download | Two files listed (newest first), preview opens, download works | Done | | |
| J-10 | Bad report name | Open `http://localhost:5000/api/reports/evil.txt` | JSON error (400), no crash | Done | | |
| J-11 | Logs tab | Logs tab after the steps above | Threshold and report actions listed with time stamps | Done | | |
| J-12 | Server stops | Stop the server with Ctrl+C while the page is open; start it again | Red banner appears, then disappears by itself | Done | | |
| J-13 | Mobile width | Narrow the browser window to about 375 px | No sideways scrolling on any tab | Done | | |
| J-14 | Local only by default | `python3 web/app.py`, then open `http://<vm-ip>:5000` from another computer | Not reachable. With `--host 0.0.0.0` it is, and a warning is printed | Done | | |
| J-15 | Process CPU | `yes > /dev/null` in a second terminal; Processes tab | `yes` is first with about 100 % (one core) | Done (97.7 %) | | |
| J-16 | Non-interactive modes | `bash server_monitor.sh --check; echo $?` and `--report; echo $?` | Table printed, exit 0 (OK) or 1 (WARNING); report file created, exit 0 | Done | | |

## Summary

| Section | Total | Passed | Failed |
|---|---|---|---|
| A Static | 2 | | |
| B Menu | 9 | | |
| C Metrics | 8 | | |
| D Thresholds | 11 | | |
| E Alerts | 4 | | |
| F Service | 6 | | |
| G Reports/logs | 8 | | |
| H Failure | 7 | | |
| I Safety/environment | 5 | | |
| J Web dashboard / automated tests | 16 | | |
| **Total** | **76** | | |

**Overall result:** ☐ All passed  ☐ Some failed (list IDs: ______________________)

**Tester signature:** ______________________
