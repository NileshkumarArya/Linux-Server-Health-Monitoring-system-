#!/usr/bin/env bash
# =============================================================================
#  Linux Server Health Monitoring & Alert System
#  File    : server_monitor.sh
#  Version : 1.1
#  Target  : Ubuntu Linux (Bash 4+), physical machine or VirtualBox VM
#
#  A menu-driven, read-only terminal tool that shows CPU, memory, disk, uptime,
#  users, processes and service status, compares CPU/RAM/disk usage with
#  user-configurable thresholds, and saves timestamped reports and logs.
#
#  Design rules (see docs/01_PRD.docx and docs/02_ARCHITECTURE.docx):
#    * Only Bash + standard Linux commands. No web server, database or cloud.
#    * Works without sudo. Writes ONLY inside its own data directory.
#    * Never uses eval, never sources the config file, never kills processes
#      or restarts services.
#
#  Launch :  bash ./server_monitor.sh
# =============================================================================

# NOTE: "set -e" is deliberately NOT used. A menu program must survive a failed
# command (for example a missing utility) and keep running.

VERSION="1.1"
APP_NAME="Linux Server Health Monitoring & Alert System"

# Files created by this program are private to the current user (rw-------).
umask 077

# -----------------------------------------------------------------------------
# 1. GLOBAL SETTINGS
# -----------------------------------------------------------------------------

# Directory that contains this script (works even if started from elsewhere).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
[ -n "$SCRIPT_DIR" ] || SCRIPT_DIR="."

# Default thresholds (percent). Used when the config file is missing/invalid.
DEFAULT_CPU_THRESHOLD=80
DEFAULT_RAM_THRESHOLD=80
DEFAULT_DISK_THRESHOLD=80

CPU_THRESHOLD=$DEFAULT_CPU_THRESHOLD
RAM_THRESHOLD=$DEFAULT_RAM_THRESHOLD
DISK_THRESHOLD=$DEFAULT_DISK_THRESHOLD

# Which filesystem is used for the disk alert. "/" = the root filesystem.
DISK_PATH="/"

# CPU is measured by taking two samples of /proc/stat this many seconds apart.
CPU_SAMPLE_SECONDS=1

# Runtime data locations (filled in by init_data_dir).
DATA_DIR=""
CONFIG_FILE=""
LOG_FILE=""
REPORT_DIR=""
DATA_OK=0      # 1 when the data directory is usable
LOG_OK=0       # 1 when the log file is writable
LOG_WARNED=0   # so a logging failure is reported only once

# Latest readings. Percentages are stored as "tenths" (integer x10) so that
# 45.3% is stored as 453. This lets Bash compare values without floating point.
# An EMPTY value means "could not be measured" (never a made-up number).
CPU_X10=""
MEM_X10=""
DISK_X10=""
MEM_TOTAL_KB=""; MEM_USED_KB=""; MEM_AVAIL_KB=""
DISK_FS=""; DISK_MOUNT=""; DISK_TOTAL_KB=""; DISK_USED_KB=""; DISK_AVAIL_KB=""
CPU_ERROR=""; MEM_ERROR=""; DISK_ERROR=""

# Last line typed by the user (set by read_input).
INPUT=""

# Colours are used only on a real terminal (and can be disabled with NO_COLOR).
COLOR=0
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    COLOR=1
fi

# -----------------------------------------------------------------------------
# 2. SMALL HELPERS (colours, time, messages, input cleaning)
# -----------------------------------------------------------------------------

# paint COLOUR_NAME TEXT  -> prints TEXT in colour (plain text if COLOR=0)
paint() {
    local name=$1; shift
    if [ "$COLOR" -eq 1 ]; then
        local code=""
        case "$name" in
            red)    code='1;31' ;;
            green)  code='1;32' ;;
            yellow) code='1;33' ;;
            cyan)   code='1;36' ;;
            bold)   code='1' ;;
        esac
        printf '\033[%sm%s\033[0m' "$code" "$*"
    else
        printf '%s' "$*"
    fi
}

# Current timestamp, e.g. 2026-10-14 15:04:05
now() {
    if command -v date >/dev/null 2>&1; then
        date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf 'unknown-time'
    else
        printf 'unknown-time'
    fi
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

msg_info()  { printf '%s\n' "$*"; }
msg_warn()  { printf '%s %s\n' "$(paint yellow '[WARNING]')" "$*" >&2; }
msg_error() { printf '%s %s\n' "$(paint red '[ERROR]')" "$*" >&2; }

# Print a section heading.
header() {
    printf '\n%s\n' "$(paint cyan "=== $* ===")"
}

# Remove leading/trailing whitespace.
trim() {
    local s=$1
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# Make user-typed text safe to show/log: replace unprintable characters and
# cut to 40 characters so a log line can never be flooded or forged.
sanitize() {
    local s=${1//[^[:print:]]/?}
    printf '%s' "${s:0:40}"
}

# Print a prompt and read one line into INPUT. Returns 1 on end-of-input
# (Ctrl+D or a closed pipe) so callers can stop instead of looping forever.
read_input() {
    local line
    printf '%s' "$1"
    if ! IFS= read -r line; then
        printf '\n'
        INPUT=""
        return 1
    fi
    INPUT=$(trim "$line")
    return 0
}

# Wait for Enter - only when running interactively (so piped tests don't hang).
pause_for_enter() {
    if [ -t 0 ]; then
        printf '\nPress Enter to return to the menu... '
        IFS= read -r _ || true
    fi
}

# -----------------------------------------------------------------------------
# 3. LOGGER
# -----------------------------------------------------------------------------

# log_msg LEVEL MESSAGE...   (LEVEL = INFO | WARN | ERROR)
# Appends "YYYY-MM-DD HH:MM:SS [LEVEL] message" to the log file. A failure to
# write the log is reported once and never stops the program.
log_msg() {
    local level=$1; shift
    local text="$*"
    text=${text//$'\n'/ }
    text=${text//$'\r'/ }
    [ "$LOG_OK" -eq 1 ] || return 0
    if ! printf '%s [%s] %s\n' "$(now)" "$level" "$text" 2>/dev/null >>"$LOG_FILE"; then
        if [ "$LOG_WARNED" -eq 0 ]; then
            LOG_WARNED=1
            msg_warn "Cannot write to log file: $LOG_FILE (check permissions / free disk space). Continuing without logging."
        fi
        return 1
    fi
    return 0
}

# -----------------------------------------------------------------------------
# 4. LOCAL STORAGE: data directory, config file
# -----------------------------------------------------------------------------

# Try to create/use one candidate data directory. Returns 0 when usable.
try_data_dir() {
    local dir=$1
    mkdir -p -- "$dir/logs" "$dir/reports" 2>/dev/null || return 1
    [ -d "$dir/logs" ] && [ -w "$dir/logs" ] || return 1
    [ -d "$dir/reports" ] && [ -w "$dir/reports" ] || return 1
    return 0
}

# Choose and prepare the data directory.
#  1. SERVER_MONITOR_DATA_DIR (if the user set it) - used exclusively
#  2. <project folder>/data
#  3. ~/.linux_server_monitor  (fallback when the project folder is read-only)
init_data_dir() {
    DATA_OK=0
    LOG_OK=0
    if [ -n "${SERVER_MONITOR_DATA_DIR:-}" ]; then
        if try_data_dir "$SERVER_MONITOR_DATA_DIR"; then
            DATA_DIR=$SERVER_MONITOR_DATA_DIR
            DATA_OK=1
        else
            DATA_DIR=$SERVER_MONITOR_DATA_DIR
            msg_error "Cannot create or write to the data directory: $DATA_DIR"
            msg_info  "  Hint: check that the path exists, you own it, and the disk is not full."
        fi
    elif try_data_dir "$SCRIPT_DIR/data"; then
        DATA_DIR="$SCRIPT_DIR/data"
        DATA_OK=1
    elif [ -n "${HOME:-}" ] && try_data_dir "$HOME/.linux_server_monitor"; then
        DATA_DIR="$HOME/.linux_server_monitor"
        DATA_OK=1
        msg_warn "Project folder is not writable; using $DATA_DIR instead."
    else
        DATA_DIR="$SCRIPT_DIR/data"
        msg_error "No writable data directory found (tried $SCRIPT_DIR/data and ~/.linux_server_monitor)."
        msg_info  "  Hint: run from a folder you own, or set SERVER_MONITOR_DATA_DIR to a writable path."
    fi

    CONFIG_FILE="$DATA_DIR/config.conf"
    LOG_FILE="$DATA_DIR/logs/app.log"
    REPORT_DIR="$DATA_DIR/reports"

    if [ "$DATA_OK" -eq 1 ]; then
        LOG_OK=1
        # Keep the log small: above 1 MiB the old log becomes app.log.1
        if have_cmd wc && [ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)" -gt 1048576 ]; then
            mv -f -- "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null
        fi
    else
        msg_warn "Running without saved settings, logs or reports (monitoring still works)."
    fi
}

# A valid threshold is a whole number from 1 to 100 (no sign, decimal or text).
valid_threshold() {
    local v=$1
    [[ $v =~ ^[1-9][0-9]{0,2}$ ]] || return 1
    (( 10#$v >= 1 && 10#$v <= 100 )) || return 1
    return 0
}

# Write the three thresholds to the config file (atomic: temp file, then move).
save_config() {
    [ "$DATA_OK" -eq 1 ] || { msg_error "Settings cannot be saved: no writable data directory."; return 1; }
    local tmp="$CONFIG_FILE.tmp.$$"
    if ! {
            printf '# Linux Server Monitor settings (whole numbers 1-100)\n'
            printf 'CPU_THRESHOLD=%s\n'  "$CPU_THRESHOLD"
            printf 'RAM_THRESHOLD=%s\n'  "$RAM_THRESHOLD"
            printf 'DISK_THRESHOLD=%s\n' "$DISK_THRESHOLD"
         } 2>/dev/null >"$tmp" || ! mv -f -- "$tmp" "$CONFIG_FILE" 2>/dev/null; then
        rm -f -- "$tmp" 2>/dev/null
        msg_error "Could not write settings file: $CONFIG_FILE"
        msg_info  "  Hint: check file permissions and free disk space."
        log_msg ERROR "Failed to write config file $CONFIG_FILE"
        return 1
    fi
    return 0
}

# Read thresholds from the config file WITHOUT sourcing it. Each line must be
# exactly KEY=NUMBER for one of the three known keys; anything else is ignored
# (and triggers a repair of this application-owned file).
load_config() {
    CPU_THRESHOLD=$DEFAULT_CPU_THRESHOLD
    RAM_THRESHOLD=$DEFAULT_RAM_THRESHOLD
    DISK_THRESHOLD=$DEFAULT_DISK_THRESHOLD

    if [ "$DATA_OK" -ne 1 ]; then
        return 0
    fi

    if [ ! -f "$CONFIG_FILE" ]; then
        msg_info "No settings file found - creating one with default thresholds (80/80/80)."
        save_config && log_msg INFO "Created default config file"
        return 0
    fi

    local line key val problem=0 got_cpu=0 got_ram=0 got_disk=0
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        [[ -z $line || $line == \#* ]] && continue
        if [[ $line =~ ^(CPU|RAM|DISK)_THRESHOLD=([0-9]{1,3})$ ]]; then
            key=${BASH_REMATCH[1]}
            val=${BASH_REMATCH[2]}
            if valid_threshold "$val"; then
                case "$key" in
                    CPU)  CPU_THRESHOLD=$val;  got_cpu=1 ;;
                    RAM)  RAM_THRESHOLD=$val;  got_ram=1 ;;
                    DISK) DISK_THRESHOLD=$val; got_disk=1 ;;
                esac
            else
                problem=1
            fi
        else
            problem=1
        fi
    done <"$CONFIG_FILE" 2>/dev/null

    if [ $got_cpu -eq 0 ] || [ $got_ram -eq 0 ] || [ $got_disk -eq 0 ]; then
        problem=1
    fi

    if [ $problem -eq 1 ]; then
        msg_warn "Settings file was missing values or contained invalid lines. Using defaults for those values and repairing the file."
        log_msg WARN "Config file invalid or incomplete; defaults applied and file repaired"
        save_config
    fi
    return 0
}

# -----------------------------------------------------------------------------
# 5. ENVIRONMENT CHECKS
# -----------------------------------------------------------------------------

check_environment() {
    local missing="" c
    if [ "$(uname -s 2>/dev/null)" != "Linux" ]; then
        msg_warn "This tool is designed for Linux. Some features may be unavailable on this system."
    fi
    if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
        msg_warn "Bash 4 or newer is recommended (found ${BASH_VERSION:-unknown})."
    fi
    for c in df ps who uptime sleep head tail; do
        have_cmd "$c" || missing="$missing $c"
    done
    if [ -n "$missing" ]; then
        msg_warn "Missing utilities:$missing - the related features will be reported as unavailable."
        log_msg WARN "Missing utilities:$missing"
    fi
    if [ ! -r /proc/stat ] || [ ! -r /proc/meminfo ]; then
        msg_warn "/proc/stat or /proc/meminfo is not readable - CPU/memory readings will be unavailable."
        log_msg WARN "/proc not fully readable"
    fi
}

# True when systemctl exists AND this machine is actually booted with systemd.
systemd_available() {
    have_cmd systemctl && [ -d /run/systemd/system ]
}

# -----------------------------------------------------------------------------
# 6. METRICS COLLECTOR  (each collect_* sets globals; empty = unavailable)
# -----------------------------------------------------------------------------

# Format a tenths value (453 -> "45.3%"); empty -> "N/A".
fmt_x10() {
    if [ -z "$1" ]; then
        printf 'N/A'
    else
        printf '%d.%d%%' $(( $1 / 10 )) $(( $1 % 10 ))
    fi
}

# Format kilobytes as MiB or GiB with one decimal, e.g. 2048 -> "2.0 MiB".
fmt_size() {
    if [ -z "$1" ]; then
        printf 'N/A'
    elif [ "$1" -ge 1048576 ]; then
        local g=$(( $1 * 10 / 1048576 ))
        printf '%d.%d GiB' $(( g / 10 )) $(( g % 10 ))
    else
        local m=$(( $1 * 10 / 1024 ))
        printf '%d.%d MiB' $(( m / 10 )) $(( m % 10 ))
    fi
}

# Read the aggregate "cpu" line of /proc/stat into CPU_TOTAL and CPU_IDLE.
# Fields: user nice system idle iowait irq softirq steal ...
read_cpu_counters() {
    local label user nice system idle iowait irq softirq steal rest v
    [ -r /proc/stat ] || return 1
    read -r label user nice system idle iowait irq softirq steal rest </proc/stat 2>/dev/null || return 1
    [ "$label" = "cpu" ] || return 1
    iowait=${iowait:-0}; irq=${irq:-0}; softirq=${softirq:-0}; steal=${steal:-0}
    for v in "$user" "$nice" "$system" "$idle" "$iowait" "$irq" "$softirq" "$steal"; do
        [[ $v =~ ^[0-9]+$ ]] || return 1
    done
    CPU_TOTAL=$(( user + nice + system + idle + iowait + irq + softirq + steal ))
    CPU_IDLE=$(( idle + iowait ))
    return 0
}

# CPU utilisation = share of NON-idle time between two /proc/stat samples
# taken CPU_SAMPLE_SECONDS apart:  busy% = (delta_total - delta_idle) / delta_total
collect_cpu() {
    CPU_X10=""; CPU_ERROR=""
    local t1 i1 t2 i2 dt di busy
    if ! have_cmd sleep; then CPU_ERROR="the 'sleep' command is missing"; return 1; fi
    if ! read_cpu_counters; then CPU_ERROR="cannot read /proc/stat"; return 1; fi
    t1=$CPU_TOTAL; i1=$CPU_IDLE
    sleep "$CPU_SAMPLE_SECONDS"
    if ! read_cpu_counters; then CPU_ERROR="cannot read /proc/stat"; return 1; fi
    t2=$CPU_TOTAL; i2=$CPU_IDLE
    dt=$(( t2 - t1 )); di=$(( i2 - i1 ))
    if [ "$dt" -le 0 ]; then CPU_ERROR="no CPU time elapsed between samples"; return 1; fi
    busy=$(( dt - di ))
    [ "$busy" -lt 0 ] && busy=0
    CPU_X10=$(( busy * 1000 / dt ))
    [ "$CPU_X10" -gt 1000 ] && CPU_X10=1000
    return 0
}

# Memory from /proc/meminfo. "Used" = MemTotal - MemAvailable (the same idea
# as the "used" column of `free`). Falls back to Free+Buffers+Cached when the
# kernel has no MemAvailable field.
collect_memory() {
    MEM_X10=""; MEM_ERROR=""
    MEM_TOTAL_KB=""; MEM_USED_KB=""; MEM_AVAIL_KB=""
    local key val rest total="" avail="" free="" buffers="" cached=""
    if [ ! -r /proc/meminfo ]; then MEM_ERROR="cannot read /proc/meminfo"; return 1; fi
    while IFS=': ' read -r key val rest; do
        case "$key" in
            MemTotal)     total=$val ;;
            MemAvailable) avail=$val ;;
            MemFree)      free=$val ;;
            Buffers)      buffers=$val ;;
            Cached)       cached=$val ;;
        esac
    done </proc/meminfo 2>/dev/null

    if [[ ! $total =~ ^[0-9]+$ ]] || [ "$total" -le 0 ]; then
        MEM_ERROR="MemTotal not found in /proc/meminfo"; return 1
    fi
    if [[ ! $avail =~ ^[0-9]+$ ]]; then
        if [[ $free =~ ^[0-9]+$ && $buffers =~ ^[0-9]+$ && $cached =~ ^[0-9]+$ ]]; then
            avail=$(( free + buffers + cached ))
        else
            MEM_ERROR="available memory not reported by the kernel"; return 1
        fi
    fi
    [ "$avail" -gt "$total" ] && avail=$total
    MEM_TOTAL_KB=$total
    MEM_AVAIL_KB=$avail
    MEM_USED_KB=$(( total - avail ))
    MEM_X10=$(( MEM_USED_KB * 1000 / total ))
    return 0
}

# Disk usage of DISK_PATH from `df -P -k` (POSIX format, 1K blocks).
# The percentage is df's own "Use%" so it can be checked against `df` directly.
collect_disk() {
    DISK_X10=""; DISK_ERROR=""
    DISK_FS=""; DISK_MOUNT=""; DISK_TOTAL_KB=""; DISK_USED_KB=""; DISK_AVAIL_KB=""
    local out line pct
    if ! have_cmd df; then DISK_ERROR="the 'df' command is missing"; return 1; fi
    out=$(df -P -k -- "$DISK_PATH" 2>/dev/null) || { DISK_ERROR="'df' failed for $DISK_PATH"; return 1; }
    line=$(printf '%s\n' "$out" | tail -n 1)
    [ -n "$line" ] || { DISK_ERROR="'df' returned no data"; return 1; }
    read -r DISK_FS DISK_TOTAL_KB DISK_USED_KB DISK_AVAIL_KB pct DISK_MOUNT <<<"$line"
    if [[ ! $pct =~ ^[0-9]+%$ ]] || [[ ! $DISK_TOTAL_KB =~ ^[0-9]+$ ]]; then
        DISK_ERROR="could not understand the output of 'df'"
        DISK_FS=""; DISK_MOUNT=""
        return 1
    fi
    pct=${pct%\%}
    DISK_X10=$(( pct * 10 ))
    return 0
}

# -----------------------------------------------------------------------------
# 7. THRESHOLD AND ALERT ENGINE
# -----------------------------------------------------------------------------

# metric_status VALUE_X10 THRESHOLD  -> prints OK, WARNING or UNAVAILABLE
# Rule (FR-09): WARNING when measured percentage >= threshold.
metric_status() {
    local value=$1 threshold=$2
    if [ -z "$value" ]; then
        printf 'UNAVAILABLE'
    elif [ "$value" -ge $(( threshold * 10 )) ]; then
        printf 'WARNING'
    else
        printf 'OK'
    fi
}

# Coloured version of a status word.
paint_status() {
    case "$1" in
        OK)      paint green  "$1" ;;
        WARNING) paint red    "$1" ;;
        *)       paint yellow "$1" ;;
    esac
}

# One row of the alert table.
print_alert_row() {
    local name=$1 value=$2 threshold=$3 status
    status=$(metric_status "$value" "$threshold")
    printf '  %-8s %-10s %-10s %s\n' "$name" "$(fmt_x10 "$value")" "${threshold}%" "$(paint_status "$status")"
    if [ "$status" = "WARNING" ]; then
        log_msg WARN "ALERT: $name usage $(fmt_x10 "$value") >= threshold ${threshold}%"
    fi
}

# Table of the latest readings (collect_* must be called first).
print_alerts() {
    printf '  %-8s %-10s %-10s %s\n' "Metric" "Current" "Threshold" "Status"
    printf '  %-8s %-10s %-10s %s\n' "------" "-------" "---------" "------"
    print_alert_row "CPU"  "$CPU_X10"  "$CPU_THRESHOLD"
    print_alert_row "RAM"  "$MEM_X10"  "$RAM_THRESHOLD"
    print_alert_row "Disk" "$DISK_X10" "$DISK_THRESHOLD"
    [ -n "$CPU_ERROR" ]  && printf '  Note: CPU unavailable - %s\n'  "$CPU_ERROR"
    [ -n "$MEM_ERROR" ]  && printf '  Note: RAM unavailable - %s\n'  "$MEM_ERROR"
    [ -n "$DISK_ERROR" ] && printf '  Note: Disk unavailable - %s\n' "$DISK_ERROR"
    return 0
}

# -----------------------------------------------------------------------------
# 8. PRINT FUNCTIONS (they only display; collect_* must be called first)
# -----------------------------------------------------------------------------

print_cpu() {
    if [ -z "$CPU_X10" ]; then
        printf '  CPU usage     : UNAVAILABLE (%s)\n' "${CPU_ERROR:-unknown error}"
    else
        printf '  CPU usage     : %s  (average over %s second(s))\n' "$(fmt_x10 "$CPU_X10")" "$CPU_SAMPLE_SECONDS"
    fi
}

print_cpu_details() {
    local model="" cores="" l1 l5 l15 key val
    if [ -r /proc/cpuinfo ]; then
        while IFS=':' read -r key val; do
            key=$(trim "$key")
            if [ "$key" = "model name" ]; then model=$(trim "$val"); break; fi
        done </proc/cpuinfo 2>/dev/null
    fi
    if have_cmd nproc; then cores=$(nproc 2>/dev/null); fi
    printf '  Processor     : %s\n' "${model:-unknown}"
    printf '  CPU cores     : %s\n' "${cores:-unknown}"
    if [ -r /proc/loadavg ] && read -r l1 l5 l15 _ </proc/loadavg 2>/dev/null; then
        printf '  Load average  : %s (1 min)  %s (5 min)  %s (15 min)\n' "$l1" "$l5" "$l15"
    else
        printf '  Load average  : unavailable\n'
    fi
}

print_memory() {
    if [ -z "$MEM_X10" ]; then
        printf '  Memory        : UNAVAILABLE (%s)\n' "${MEM_ERROR:-unknown error}"
        return 0
    fi
    printf '  Total RAM     : %s\n' "$(fmt_size "$MEM_TOTAL_KB")"
    printf '  Used RAM      : %s\n' "$(fmt_size "$MEM_USED_KB")"
    printf '  Available RAM : %s\n' "$(fmt_size "$MEM_AVAIL_KB")"
    printf '  RAM usage     : %s\n' "$(fmt_x10 "$MEM_X10")"
}

print_disk() {
    if [ -z "$DISK_X10" ]; then
        printf '  Disk          : UNAVAILABLE (%s)\n' "${DISK_ERROR:-unknown error}"
        return 0
    fi
    printf '  Filesystem    : %s\n' "$DISK_FS"
    printf '  Mount point   : %s\n' "$DISK_MOUNT"
    printf '  Size          : %s\n' "$(fmt_size "$DISK_TOTAL_KB")"
    printf '  Used          : %s\n' "$(fmt_size "$DISK_USED_KB")"
    printf '  Available     : %s\n' "$(fmt_size "$DISK_AVAIL_KB")"
    printf '  Disk usage    : %s  (as reported by df)\n' "$(fmt_x10 "$DISK_X10")"
}

# All mounted filesystems in human-readable form (virtual ones filtered out).
print_all_filesystems() {
    local out
    if ! have_cmd df; then printf '  (df command not available)\n'; return 0; fi
    out=$(df -P -h -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null)
    [ -n "$out" ] || out=$(df -P -h 2>/dev/null)
    if [ -z "$out" ]; then
        printf '  (df returned no data)\n'
    else
        printf '%s\n' "$out" | sed 's/^/  /'
    fi
}

print_uptime() {
    local pretty="" l1 l5 l15
    if have_cmd uptime; then pretty=$(uptime -p 2>/dev/null); fi
    printf '  Date / time   : %s\n' "$(now)"
    if [ -n "$pretty" ]; then
        printf '  Uptime        : %s\n' "${pretty#up }"
    elif have_cmd uptime; then
        printf '  Uptime        : %s\n' "$(uptime 2>/dev/null)"
    else
        printf '  Uptime        : unavailable (uptime command missing)\n'
    fi
    if [ -r /proc/loadavg ] && read -r l1 l5 l15 _ </proc/loadavg 2>/dev/null; then
        printf '  Load average  : %s (1 min)  %s (5 min)  %s (15 min)\n' "$l1" "$l5" "$l15"
    fi
}

print_users() {
    local out
    if ! have_cmd who; then printf '  (who command not available)\n'; return 0; fi
    out=$(who 2>/dev/null)
    if [ -z "$out" ]; then
        printf '  No users are currently logged in.\n'
    else
        printf '%s\n' "$out" | sed 's/^/  /'
    fi
}

# Top 10 processes by CPU use.
print_processes() {
    local out
    if ! have_cmd ps; then printf '  (ps command not available)\n'; return 0; fi
    out=$(ps -eo pid,user:14,pcpu,pmem,comm --sort=-pcpu 2>/dev/null)
    if [ -z "$out" ]; then
        printf '  (could not read the process list)\n'
    else
        printf '%s\n' "$out" | head -n 11 | sed 's/^/  /'
        printf '  (CPU%% = average since the process started, a limit of ps)\n'
    fi
}

print_os_info() {
    local os="" key val
    if [ -r /etc/os-release ]; then
        # Parsed line by line - the file is NOT sourced.
        while IFS='=' read -r key val; do
            if [ "$key" = "PRETTY_NAME" ]; then
                val=${val#\"}; val=${val%\"}
                os=$val
                break
            fi
        done </etc/os-release 2>/dev/null
    fi
    printf '  Host name     : %s\n' "${HOSTNAME:-unknown}"
    printf '  Operating sys : %s\n' "${os:-unknown}"
    printf '  Kernel        : %s\n' "$(uname -r 2>/dev/null || echo unknown)"
    printf '  Current user  : %s\n' "${USER:-$(id -un 2>/dev/null || echo unknown)}"
}

# -----------------------------------------------------------------------------
# 9. MENU FEATURES (one function per menu item)
# -----------------------------------------------------------------------------

feature_summary() {
    log_msg INFO "Menu: system health summary"
    header "System Health Summary"
    printf '  (measuring CPU for %s second(s)...)\n' "$CPU_SAMPLE_SECONDS"
    collect_cpu; collect_memory; collect_disk
    print_os_info
    print_uptime
    header "Usage vs thresholds"
    print_alerts
}

feature_cpu() {
    log_msg INFO "Menu: CPU details"
    header "CPU Details"
    printf '  (measuring CPU for %s second(s)...)\n' "$CPU_SAMPLE_SECONDS"
    collect_cpu
    print_cpu
    print_cpu_details
    [ -z "$CPU_X10" ] && log_msg ERROR "CPU reading unavailable: $CPU_ERROR"
    printf '\n  Method: two samples of /proc/stat; usage = non-idle time / total time.\n'
}

feature_memory() {
    log_msg INFO "Menu: memory details"
    header "Memory Details"
    collect_memory
    print_memory
    [ -z "$MEM_X10" ] && log_msg ERROR "Memory reading unavailable: $MEM_ERROR"
    printf '\n  Method: /proc/meminfo; used = MemTotal - MemAvailable.\n'
}

feature_disk() {
    log_msg INFO "Menu: disk usage"
    header "Disk Usage (filesystem holding $DISK_PATH)"
    collect_disk
    print_disk
    [ -z "$DISK_X10" ] && log_msg ERROR "Disk reading unavailable: $DISK_ERROR"
    header "All mounted filesystems"
    print_all_filesystems
}

feature_uptime_users() {
    log_msg INFO "Menu: uptime, users and processes"
    header "Uptime and Load"
    print_uptime
    header "Logged-in Users"
    print_users
    header "Top 10 Processes by CPU"
    print_processes
}

# Service names may contain letters, digits and  @ . _ : -  only, must start
# with a letter/digit and be at most 128 characters. This blocks option-like
# input ("-x") and anything containing spaces, quotes, ; | & $ ` etc.
valid_service_name() {
    local re='^[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}$'
    [[ $1 =~ $re ]]
}

feature_service() {
    log_msg INFO "Menu: service check"
    header "Check a System Service"
    if ! have_cmd systemctl; then
        msg_warn "'systemctl' was not found. Service checks are unavailable on this system."
        log_msg WARN "Service check unavailable: systemctl missing"
        return 0
    fi
    if ! systemd_available; then
        msg_warn "systemd is not running on this system (for example a container or WSL1). Service checks are unavailable."
        log_msg WARN "Service check unavailable: systemd not running"
        return 0
    fi
    read_input "Enter service name (e.g. ssh, cron): " || return 0
    local name=$INPUT
    if [ -z "$name" ]; then
        msg_error "Service name cannot be empty."
        log_msg WARN "Service check: empty name"
        return 0
    fi
    if ! valid_service_name "$name"; then
        msg_error "Invalid service name '$(sanitize "$name")'. Use only letters, digits and . _ - @ : (max 128 characters)."
        log_msg WARN "Service check: invalid name rejected: $(sanitize "$name")"
        return 0
    fi

    local load active enabled
    load=$(systemctl show --property=LoadState -- "$name" 2>/dev/null)
    load=${load#LoadState=}
    if [ "$load" = "not-found" ]; then
        msg_error "Service '$name' was not found on this system."
        log_msg WARN "Service check: $name not found"
        return 0
    fi
    active=$(systemctl is-active -- "$name" 2>/dev/null);  [ -n "$active" ]  || active="unknown"
    enabled=$(systemctl is-enabled -- "$name" 2>/dev/null); [ -n "$enabled" ] || enabled="unknown"

    printf '  Service       : %s\n' "$name"
    printf '  Active state  : %s\n' "$active"
    printf '  Start at boot : %s\n' "$enabled"
    log_msg INFO "Service check: $name active=$active enabled=$enabled"
    if [ "$active" != "active" ]; then
        printf '  Note: this program only reports status; it never starts or restarts services.\n'
    fi
}

# Change one threshold. Saves to disk first-class: if saving fails the old
# value is restored so memory and file never disagree.
update_threshold() {
    local key=$1 new=$2 old
    case "$key" in
        CPU)  old=$CPU_THRESHOLD;  CPU_THRESHOLD=$new ;;
        RAM)  old=$RAM_THRESHOLD;  RAM_THRESHOLD=$new ;;
        DISK) old=$DISK_THRESHOLD; DISK_THRESHOLD=$new ;;
        *)    return 1 ;;
    esac
    if save_config; then
        msg_info "$key threshold set to ${new}%."
        log_msg INFO "Threshold changed: $key $old -> $new"
        return 0
    fi
    case "$key" in
        CPU)  CPU_THRESHOLD=$old ;;
        RAM)  RAM_THRESHOLD=$old ;;
        DISK) DISK_THRESHOLD=$old ;;
    esac
    msg_error "Threshold NOT changed (still ${old}%)."
    return 1
}

prompt_threshold() {
    local key=$1
    read_input "Enter new $key threshold (whole number 1-100): " || return 1
    if valid_threshold "$INPUT"; then
        update_threshold "$key" "$INPUT"
    else
        msg_error "Invalid value '$(sanitize "$INPUT")'. Enter a whole number from 1 to 100 (no decimals, signs or letters). Nothing was changed."
        log_msg WARN "Invalid $key threshold rejected: $(sanitize "$INPUT")"
    fi
    return 0
}

feature_thresholds() {
    log_msg INFO "Menu: configure thresholds"
    while true; do
        header "Configure Alert Thresholds"
        printf '  Current: CPU = %s%%   RAM = %s%%   Disk = %s%%\n\n' "$CPU_THRESHOLD" "$RAM_THRESHOLD" "$DISK_THRESHOLD"
        printf '  1) Change CPU threshold\n'
        printf '  2) Change RAM threshold\n'
        printf '  3) Change Disk threshold\n'
        printf '  4) Reset all to defaults (80/80/80)\n'
        printf '  5) Back to main menu\n'
        read_input "Choose [1-5]: " || return 0
        case "$INPUT" in
            1) prompt_threshold CPU  || return 0 ;;
            2) prompt_threshold RAM  || return 0 ;;
            3) prompt_threshold DISK || return 0 ;;
            4)
                local o1=$CPU_THRESHOLD o2=$RAM_THRESHOLD o3=$DISK_THRESHOLD
                CPU_THRESHOLD=$DEFAULT_CPU_THRESHOLD
                RAM_THRESHOLD=$DEFAULT_RAM_THRESHOLD
                DISK_THRESHOLD=$DEFAULT_DISK_THRESHOLD
                if save_config; then
                    msg_info "Thresholds reset to defaults."
                    log_msg INFO "Thresholds reset to defaults"
                else
                    CPU_THRESHOLD=$o1; RAM_THRESHOLD=$o2; DISK_THRESHOLD=$o3
                    msg_error "Thresholds NOT changed."
                fi
                ;;
            5) return 0 ;;
            *) msg_error "Invalid choice '$(sanitize "$INPUT")'. Please enter a number from 1 to 5." ;;
        esac
    done
}

feature_alerts() {
    log_msg INFO "Menu: check thresholds / alerts"
    header "Threshold Check"
    printf '  (measuring CPU for %s second(s)...)\n' "$CPU_SAMPLE_SECONDS"
    collect_cpu; collect_memory; collect_disk
    print_alerts
    printf '\n  Rule: WARNING when current usage is greater than or equal to the threshold.\n'
}

# ---- report ----------------------------------------------------------------

# Writes the report text to standard output (the caller redirects it to a file).
build_report() {
    printf '%s\n' "LINUX SERVER HEALTH REPORT"
    printf '%s\n' "=========================="
    printf 'Generated : %s\n' "$(now)"
    printf 'Tool      : %s v%s\n' "$APP_NAME" "$VERSION"
    printf '\n[SYSTEM]\n'
    print_os_info
    printf '\n[CPU]\n'
    collect_cpu; print_cpu; print_cpu_details
    printf '\n[MEMORY]\n'
    collect_memory; print_memory
    printf '\n[DISK - filesystem holding %s]\n' "$DISK_PATH"
    collect_disk; print_disk
    printf '\n[ALL FILESYSTEMS]\n'
    print_all_filesystems
    printf '\n[UPTIME AND LOAD]\n'
    print_uptime
    printf '\n[LOGGED-IN USERS]\n'
    print_users
    printf '\n[TOP 10 PROCESSES BY CPU]\n'
    print_processes
    printf '\n[ALERTS]\n'
    print_alerts
    printf '\n--- end of report ---\n'
}

feature_report() {
    log_msg INFO "Menu: generate report"
    header "Generate Report"
    if [ "$DATA_OK" -ne 1 ]; then
        msg_error "Cannot save a report: no writable data directory ($DATA_DIR)."
        msg_info  "  Hint: check permissions or set SERVER_MONITOR_DATA_DIR to a writable folder."
        log_msg ERROR "Report not saved: data directory unusable"
        return 1
    fi
    printf '  (collecting data, this takes about %s second(s)...)\n' "$CPU_SAMPLE_SECONDS"

    local ts file n=0 saved_color=$COLOR
    ts=$(date '+%Y%m%d_%H%M%S' 2>/dev/null) || ts="unknown"
    file="$REPORT_DIR/report_${ts}.txt"
    # Never overwrite an earlier report taken in the same second.
    while [ -e "$file" ]; do
        n=$(( n + 1 ))
        file="$REPORT_DIR/report_${ts}_${n}.txt"
    done

    COLOR=0                                   # plain text inside the file
    { build_report; } 2>/dev/null >"$file"
    local rc=$?
    COLOR=$saved_color

    if [ $rc -ne 0 ] || [ ! -s "$file" ]; then
        rm -f -- "$file" 2>/dev/null
        msg_error "Could not write the report to: $file"
        msg_info  "  Hint: check folder permissions and free disk space (df -h)."
        log_msg ERROR "Report write failed: $file"
        return 1
    fi
    msg_info "Report saved: $file"
    log_msg INFO "Report generated: $file"
    return 0
}

# ---- history ---------------------------------------------------------------

# Fills the array REPORT_LIST with report file paths, newest first.
list_reports() {
    REPORT_LIST=()
    local f
    [ -d "$REPORT_DIR" ] || return 0
    # File names contain a timestamp, so alphabetical order = chronological.
    for f in "$REPORT_DIR"/report_*.txt; do
        [ -f "$f" ] && REPORT_LIST=("$f" "${REPORT_LIST[@]}")
    done
    return 0
}

feature_history() {
    log_msg INFO "Menu: view logs/reports"
    while true; do
        header "Recent Logs and Reports"
        printf '  1) Show last 20 log entries\n'
        printf '  2) List saved reports (newest 10)\n'
        printf '  3) Open a saved report\n'
        printf '  4) Back to main menu\n'
        read_input "Choose [1-4]: " || return 0
        case "$INPUT" in
            1) show_log_tail ;;
            2) show_report_list ;;
            3) open_report ;;
            4) return 0 ;;
            *) msg_error "Invalid choice '$(sanitize "$INPUT")'. Please enter a number from 1 to 4." ;;
        esac
    done
}

show_log_tail() {
    header "Last 20 log entries ($LOG_FILE)"
    if [ "$DATA_OK" -ne 1 ] || [ ! -s "$LOG_FILE" ]; then
        printf '  The log is empty or not available.\n'
    else
        tail -n 20 -- "$LOG_FILE" 2>/dev/null | sed 's/^/  /'
    fi
}

show_report_list() {
    local i f
    list_reports
    header "Saved reports ($REPORT_DIR)"
    if [ ${#REPORT_LIST[@]} -eq 0 ]; then
        printf '  No reports saved yet. Use menu option 9 to create one.\n'
        return 0
    fi
    for (( i = 0; i < ${#REPORT_LIST[@]} && i < 10; i++ )); do
        f=${REPORT_LIST[$i]}
        printf '  %2d) %s\n' $(( i + 1 )) "${f##*/}"
    done
}

open_report() {
    local n
    list_reports
    if [ ${#REPORT_LIST[@]} -eq 0 ]; then
        printf '  No reports saved yet. Use menu option 9 to create one.\n'
        return 0
    fi
    show_report_list
    read_input "Enter report number to open (1-10): " || return 0
    if [[ $INPUT =~ ^[0-9]+$ ]] && [ "$INPUT" -ge 1 ] && [ "$INPUT" -le 10 ] && [ "$INPUT" -le ${#REPORT_LIST[@]} ]; then
        n=$(( INPUT - 1 ))
        header "${REPORT_LIST[$n]##*/}"
        sed 's/^/  /' < "${REPORT_LIST[$n]}" 2>/dev/null || msg_error "Could not read that report."
        log_msg INFO "Report viewed: ${REPORT_LIST[$n]##*/}"
    else
        msg_error "Invalid report number '$(sanitize "$INPUT")'."
        log_msg WARN "Invalid report number: $(sanitize "$INPUT")"
    fi
}

# -----------------------------------------------------------------------------
# 10. MENU / CONTROLLER
# -----------------------------------------------------------------------------

print_menu() {
    if [ -t 1 ] && have_cmd clear; then clear 2>/dev/null; fi
    printf '\n%s\n' "$(paint bold "╔══════════════════════════════════════════════════════╗")"
    printf '%s\n'   "$(paint bold "║   LINUX SERVER HEALTH MONITORING & ALERT SYSTEM      ║")"
    printf '%s\n'   "$(paint bold "╚══════════════════════════════════════════════════════╝")"
    printf '  Thresholds: CPU %s%%  RAM %s%%  Disk %s%%\n\n' "$CPU_THRESHOLD" "$RAM_THRESHOLD" "$DISK_THRESHOLD"
    printf '   1) Show system health summary\n'
    printf '   2) Show CPU details\n'
    printf '   3) Show memory details\n'
    printf '   4) Show disk usage\n'
    printf '   5) Show uptime, logged-in users and top processes\n'
    printf '   6) Check a system service\n'
    printf '   7) Configure alert thresholds\n'
    printf '   8) Check all thresholds / show alerts\n'
    printf '   9) Generate a report\n'
    printf '  10) View recent logs/reports\n'
    printf '  11) Exit\n\n'
}

say_goodbye() {
    log_msg INFO "Application exited"
    printf '\nThank you for using the Linux Server Health Monitor. Goodbye!\n'
}

# Ctrl+C: leave cleanly instead of dumping a half-printed screen.
handle_interrupt() {
    printf '\n'
    log_msg INFO "Interrupted by user (Ctrl+C)"
    say_goodbye
    exit 130
}

main_loop() {
    local choice
    while true; do
        print_menu
        if ! read_input "Enter your choice [1-11]: "; then
            log_msg INFO "End of input reached"
            break
        fi
        choice=$INPUT
        case "$choice" in
            1)  feature_summary ;;
            2)  feature_cpu ;;
            3)  feature_memory ;;
            4)  feature_disk ;;
            5)  feature_uptime_users ;;
            6)  feature_service ;;
            7)  feature_thresholds ;;
            8)  feature_alerts ;;
            9)  feature_report ;;
            10) feature_history ;;
            11) break ;;
            "")
                msg_error "You did not enter anything. Please type a number from 1 to 11."
                ;;
            *)
                msg_error "Invalid choice '$(sanitize "$choice")'. Please type a number from 1 to 11."
                log_msg WARN "Invalid menu choice: $(sanitize "$choice")"
                ;;
        esac
        pause_for_enter
    done
}

# Non-interactive check for cron:  server_monitor.sh --check
# Prints the alert table, logs any WARNING, exit status 0 = all OK, 1 = warning.
run_check() {
    collect_cpu; collect_memory; collect_disk
    header "Threshold Check ($(now))"
    print_alerts
    log_msg INFO "Non-interactive check (--check)"
    local c m d r
    c=$(metric_status "$CPU_X10"  "$CPU_THRESHOLD")
    m=$(metric_status "$MEM_X10"  "$RAM_THRESHOLD")
    d=$(metric_status "$DISK_X10" "$DISK_THRESHOLD")
    for r in "$c" "$m" "$d"; do
        [ "$r" = "WARNING" ] && return 1
    done
    return 0
}

usage() {
    cat <<EOF
$APP_NAME v$VERSION

Usage: bash ./server_monitor.sh [option]

Options:
  -h, --help       Show this help and exit
  -v, --version    Show the version and exit
  --check          Non-interactive: print the alert table and exit
                   (exit status 0 = all OK, 1 = at least one WARNING)
  --report         Non-interactive: save one report and exit
                   (exit status 0 = saved, 1 = failed)

Both non-interactive options are meant for cron, for example:
  */5  * * * * bash ~/linux-server-monitor/server_monitor.sh --check  >/dev/null 2>&1
  */15 * * * * bash ~/linux-server-monitor/server_monitor.sh --report >/dev/null 2>&1

Environment:
  SERVER_MONITOR_DATA_DIR   Use this folder for config, logs and reports
                            (default: ./data next to the script)
  NO_COLOR                  Set to any value to disable coloured output
EOF
}

main() {
    local mode="menu"
    case "${1:-}" in
        -h|--help)    usage; exit 0 ;;
        -v|--version) printf '%s v%s\n' "$APP_NAME" "$VERSION"; exit 0 ;;
        --check)      mode="check" ;;
        --report)     mode="report" ;;
        "")           ;;
        *)            msg_error "Unknown option: $(sanitize "$1")"; usage; exit 2 ;;
    esac

    init_data_dir
    check_environment
    load_config

    if [ "$mode" = "check" ]; then
        log_msg INFO "Started in --check mode (user ${USER:-$(id -un 2>/dev/null)})"
        run_check
        exit $?
    fi
    if [ "$mode" = "report" ]; then
        log_msg INFO "Started in --report mode (user ${USER:-$(id -un 2>/dev/null)})"
        feature_report
        exit $?
    fi

    trap handle_interrupt INT
    log_msg INFO "Application started (v$VERSION, user ${USER:-$(id -un 2>/dev/null)}, data dir $DATA_DIR)"
    main_loop
    say_goodbye
    exit 0
}

main "$@"
