#!/usr/bin/env python3
"""FireWire DV deck transport, plus a detached live preview window."""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
PID_PATH = os.path.join(RUNTIME, "wouldja-dv.pid")
LOG_PATH = os.path.join(RUNTIME, "wouldja-dv.log")
SOCK_PATH = os.path.join(RUNTIME, "wouldja-dv-mpv.sock")
CAPTURE_PATH = os.path.join(RUNTIME, "wouldja-dv.capture")
STATE_PATH = os.path.expanduser("~/.config/wouldja-dv/state.json")
FIREWIRE = "/sys/bus/firewire/devices"

TRANSPORTS = {
    "play": ["dvcont", "play"],
    "pause": ["dvcont", "pause"],
    "stop": ["dvcont", "stop"],
    "rewind": ["dvcont", "rewind"],
    "ff": ["dvcont", "ff"],
    "reverse": ["dvcont", "reverse"],
    "next": ["dvcont", "next"],
    "prev": ["dvcont", "prev"],
}

# dvgrab must not send its own play/stop, or it fights the transport buttons.
# The pipeline is its own session so closing the panel does not kill it.
# tee --output-error=exit-nopipe keeps writing the file if the preview window closes.
# A later stop must not trust the saved pid alone: the pid file also stores the
# kernel start time, and signals go through pidfds only after that still matches.
PIPELINE_SCRIPT = r"""
set -o pipefail
mute=()
if [ "$DV_MUTE" = "yes" ]; then
  mute+=(--mute=yes)
fi
mpv=(
  mpv
  --no-terminal
  --force-window=immediate
  --title=DV
  --wayland-app-id=io.github.wouldja.dv
  --profile=low-latency
  --cache=no
  # low-latency sets demuxer-lavf-probe-info=nostreams, which leaves the
  # DV PCM track unselected so the preview window stays silent.
  --demuxer-lavf-probe-info=yes
  --demuxer-lavf-analyzeduration=0.5
  --aid=1
  --deinterlace=yes
  --geometry=720x540
  --autofit=720x540
  --input-ipc-server="$DV_SOCK"
  --demuxer-lavf-format=dv
  "${mute[@]}"
  -
)
grab=(dvgrab -noavc -buffers 12 -format raw -)
if [ -n "$DV_OUT" ] && [ "$DV_PREVIEW" = "yes" ]; then
  if [ "$DV_APPEND" = "yes" ]; then
    "${grab[@]}" 2>>"$DV_LOG" | tee --output-error=exit-nopipe -a "$DV_OUT" | "${mpv[@]}"
  else
    "${grab[@]}" 2>>"$DV_LOG" | tee --output-error=exit-nopipe "$DV_OUT" | "${mpv[@]}"
  fi
elif [ -n "$DV_OUT" ]; then
  if [ "$DV_APPEND" = "yes" ]; then
    "${grab[@]}" >>"$DV_OUT" 2>>"$DV_LOG"
  else
    "${grab[@]}" >"$DV_OUT" 2>>"$DV_LOG"
  fi
else
  "${grab[@]}" 2>>"$DV_LOG" | "${mpv[@]}"
fi
"""


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def load_state():
    state = {"sound": False, "lastCapture": ""}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return state
    if isinstance(raw, dict):
        state["sound"] = raw.get("sound") is True
        last = raw.get("lastCapture")
        if isinstance(last, str):
            state["lastCapture"] = last
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "sound": state.get("sound") is True,
                "lastCapture": state.get("lastCapture") or "",
            },
            handle,
        )
        handle.write("\n")
    os.replace(temporary, STATE_PATH)


def videos_dir():
    try:
        completed = subprocess.run(
            ["xdg-user-dir", "VIDEOS"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        path = (completed.stdout or "").strip()
        if completed.returncode == 0 and path and path != os.path.expanduser("~"):
            return path
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.expanduser("~/Videos")


def new_capture_path():
    folder = videos_dir()
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(folder, f"dv-{stamp}.dv")


def firewire_deck():
    try:
        names = sorted(os.listdir(FIREWIRE))
    except OSError:
        return None
    for name in names:
        if not name.startswith("fw") or "." in name:
            continue
        path = os.path.join(FIREWIRE, name)
        vendor = read_text(os.path.join(path, "vendor_name"))
        model = read_text(os.path.join(path, "model_name"))
        if not model or model == "Juju" or vendor == "Linux Firewire":
            continue
        return {"vendor": vendor, "model": model, "guid": read_text(os.path.join(path, "guid"))}
    return None


def run_dvcont(args, timeout=2.0):
    try:
        completed = subprocess.run(
            ["dvcont", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "dvcont is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "the deck did not answer"
    text = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, text.strip(), ""


def classify(text):
    lowered = text.lower()
    if not text or "no av/c" in lowered or "couldn't set port" in lowered or "invalid argument" in lowered:
        return "missing", "NO DECK"
    if "recording" in lowered:
        return "recording", "RECORD"
    if "paused" in lowered:
        return "paused", "PAUSED"
    if "reverse" in lowered and "play" in lowered:
        return "reverse", "REVERSE"
    if "playing" in lowered:
        return "playing", "PLAYING"
    if "rewind" in lowered or ("reverse" in lowered and "wind" in lowered):
        return "rewinding", "REWIND"
    if "forward" in lowered or "ff" in lowered:
        return "winding", "FORWARD"
    if "stopped" in lowered:
        return "stopped", "STOPPED"
    return "unknown", text.strip().upper() or "UNKNOWN"


def sane_timecode(text):
    match = re.search(r"(\d{2}):(\d{2}):(\d{2})[:;](\d{2})", text or "")
    if not match:
        return ""
    hours, minutes, seconds, frames = (int(part) for part in match.groups())
    if hours > 23 or minutes > 59 or seconds > 59 or frames > 29:
        return ""
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


PIPELINE_MARKER = "io.github.wouldja.dv"


def read_pid_record():
    parts = read_text(PID_PATH).split()
    if not parts:
        return 0, None
    try:
        pid = int(parts[0])
    except ValueError:
        return 0, None
    if len(parts) < 2:
        return pid, None
    try:
        return pid, int(parts[1])
    except ValueError:
        return pid, None


def write_pid_record(pid, starttime):
    temporary = PID_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(f"{pid} {starttime}\n")
    os.replace(temporary, PID_PATH)


def process_stat(pid):
    """Return ppid, pgrp, and starttime. starttime is the kernel boot tick."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    # After comm: state, ppid, pgrp, ..., starttime is field 22 (index 19).
    if len(fields) < 20:
        return None
    try:
        return {
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "starttime": int(fields[19]),
        }
    except ValueError:
        return None


def process_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def pipeline_identity(pid, expected_start):
    """The saved PID is ours only when it is still that process group leader.

    A reused PID fails the start-time check. The command line must also be
    this plugin's dvgrab pipeline, so a matching start time alone is not enough.
    """
    if pid <= 0:
        return None
    stat = process_stat(pid)
    if not stat or stat["pgrp"] != pid:
        return None
    if expected_start is not None and stat["starttime"] != expected_start:
        return None
    command = process_cmdline(pid)
    if PIPELINE_MARKER not in command or "dvgrab" not in command:
        return None
    return stat


def clear_pid_record():
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def pipeline_running():
    pid, starttime = read_pid_record()
    ident = pipeline_identity(pid, starttime)
    if ident:
        if starttime is None:
            write_pid_record(pid, ident["starttime"])
        return True
    if pid:
        clear_pid_record()
    return False


def capture_target():
    path = read_text(CAPTURE_PATH)
    return path if path else ""


def recover_capture():
    """The tee process is the source of truth if the marker file was lost."""
    if capture_target() or not pipeline_running():
        return
    pid, _starttime = read_pid_record()
    try:
        listing = subprocess.check_output(
            ["ps", "-o", "args=", "--ppid", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    for line in listing.splitlines():
        if "tee " not in line:
            continue
        for part in line.split():
            if part.endswith(".dv") and os.path.isfile(part):
                with open(CAPTURE_PATH, "w", encoding="utf-8") as handle:
                    handle.write(part)
                return


def preview_up():
    if not os.path.exists(SOCK_PATH):
        return False
    return mpv_command(["get_property", "filename"]) is not None


def snapshot(message=""):
    deck = firewire_deck()
    code, text, error = run_dvcont(["status"])
    transport, label = classify(text)
    connected = deck is not None and transport != "missing"
    if deck is None:
        transport, label = "missing", "NO DECK"
        connected = False
    timecode = ""
    if connected:
        tc_code, tc_text, tc_error = run_dvcont(["timecode"], timeout=1.2)
        if tc_code == 0:
            timecode = sane_timecode(tc_text)
        elif tc_error and not error:
            error = tc_error
    state = load_state()
    recover_capture()
    running = pipeline_running()
    target = capture_target()
    recording = bool(target) and running
    if target and not running:
        state["lastCapture"] = target
        save_state(state)
        try:
            os.remove(CAPTURE_PATH)
        except OSError:
            pass
        target = ""
        recording = False
    capture_path = target or (state.get("lastCapture") or "")
    capture_bytes = 0
    if recording:
        try:
            capture_bytes = os.path.getsize(target)
        except OSError:
            capture_bytes = 0
    if error and not message:
        message = error
    elif code not in (0, 127) and text and transport == "missing" and not message:
        message = text.splitlines()[0][:180]
    return {
        "connected": connected,
        "vendor": deck["vendor"] if deck else "",
        "model": deck["model"] if deck else "",
        "guid": deck["guid"] if deck else "",
        "transport": transport,
        "label": label,
        "statusText": text.splitlines()[0] if text else "",
        "timecode": timecode,
        "live": preview_up(),
        "sound": state.get("sound") is True,
        "recording": recording,
        "capturePath": capture_path if recording or capture_path else "",
        "captureBytes": capture_bytes,
        "message": message,
    }


def emit(status):
    json.dump(status, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def transport(name):
    command = TRANSPORTS.get(name)
    if not command:
        emit(snapshot(f"unknown transport {name}"))
        return 2
    current = snapshot()
    # play while already playing toggles slow motion on many decks.
    if name == "play" and current["transport"] == "playing":
        emit(current)
        return 0
    code, text, error = run_dvcont(command[1:])
    message = error or ("" if code == 0 else text.splitlines()[0][:180] if text else "transport command failed")
    emit(snapshot(message))
    return 0 if code == 0 else 1


def mpv_command(command):
    if not os.path.exists(SOCK_PATH):
        return None
    payload = json.dumps({"command": command}) + "\n"
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.4)
        client.connect(SOCK_PATH)
        client.sendall(payload.encode())
        data = client.recv(4096)
        client.close()
    except OSError:
        return None
    try:
        return json.loads(data.decode() or "{}")
    except json.JSONDecodeError:
        return None


def mpv_set_mute(muted):
    reply = mpv_command(["set_property", "mute", bool(muted)])
    return isinstance(reply, dict) and reply.get("error") == "success"


def mpv_quit():
    return mpv_command(["quit"]) is not None


def start_pipeline(preview, record_path="", append=False):
    if preview and shutil.which("mpv") is None:
        return "mpv is not installed"
    if shutil.which("dvgrab") is None:
        return "dvgrab is not installed"
    if record_path or preview:
        pass
    else:
        return ""
    try:
        os.remove(SOCK_PATH)
    except OSError:
        pass
    log = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    env["DV_LOG"] = LOG_PATH
    env["DV_SOCK"] = SOCK_PATH
    env["DV_MUTE"] = "no" if load_state().get("sound") else "yes"
    env["DV_PREVIEW"] = "yes" if preview else "no"
    env["DV_OUT"] = record_path or ""
    env["DV_APPEND"] = "yes" if append else "no"
    process = subprocess.Popen(
        ["bash", "-c", PIPELINE_SCRIPT],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    # Popen returns the child pid. Until exec, its command line can still be
    # ours, so wait until this pid is the dvgrab shell. Never signal it unless
    # it stayed our child for that whole wait: a reused pid has a new parent
    # or a new start time, and killing it would hit whatever took the number.
    born = None
    ident = None
    for _ in range(50):
        stat = process_stat(process.pid)
        if not stat or stat["ppid"] != os.getpid() or stat["pgrp"] != process.pid:
            return "the preview process did not start"
        if born is None:
            born = stat["starttime"]
        elif stat["starttime"] != born:
            return "the preview process did not start"
        ident = pipeline_identity(process.pid, born)
        if ident:
            break
        time.sleep(0.01)
    if not ident:
        signal_started_group(process.pid, born, signal.SIGKILL)
        return "the preview process did not start"
    write_pid_record(process.pid, ident["starttime"])
    if record_path:
        with open(CAPTURE_PATH, "w", encoding="utf-8") as handle:
            handle.write(record_path)
    else:
        try:
            os.remove(CAPTURE_PATH)
        except OSError:
            pass
    return ""


def process_children(pid):
    path = f"/proc/{pid}/task/{pid}/children"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parts = handle.read().split()
    except OSError:
        parts = None
    if parts is None:
        found = []
        try:
            names = os.listdir("/proc")
        except OSError:
            return []
        for name in names:
            if not name.isdigit():
                continue
            stat = process_stat(int(name))
            if stat and stat["ppid"] == pid:
                found.append(int(name))
        return found
    children = []
    for part in parts:
        try:
            children.append(int(part))
        except ValueError:
            continue
    return children


def open_verified_pidfd(pid, starttime, pgrp=None, ppid=None):
    """A pidfd for pid, or None if that pid no longer has this start time.

    The start time is read before and after the open. A pid reused in
    between belongs to a different start time, so its fd is closed unused.
    """

    def matches(stat):
        if not stat or stat["starttime"] != starttime:
            return False
        if pgrp is not None and stat["pgrp"] != pgrp:
            return False
        if ppid is not None and stat["ppid"] != ppid:
            return False
        return True

    if not matches(process_stat(pid)):
        return None
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        return None
    if not matches(process_stat(pid)):
        os.close(pidfd)
        return None
    return pidfd


def collect_descendants(leader, leader_start):
    """Child processes still in this group, as (pid, starttime, ppid)."""
    accepted = {leader: leader_start}
    found = []
    stack = [leader]
    while stack:
        current = stack.pop()
        parent_start = accepted.get(current)
        if parent_start is None:
            continue
        parent = process_stat(current)
        if not parent or parent["starttime"] != parent_start:
            continue
        for child in process_children(current):
            if child in accepted:
                continue
            stat = process_stat(child)
            if not stat or stat["ppid"] != current or stat["pgrp"] != leader:
                continue
            accepted[child] = stat["starttime"]
            found.append((child, stat["starttime"], current))
            stack.append(child)
    return found


def signal_started_group(pid, starttime, sig):
    """Signal the group leader and its descendants, or signal nothing.

    Callers must already know this start time. Each pidfd is opened only
    while that start time still matches, and the signal is sent to the fd.
    """
    leader_fd = open_verified_pidfd(pid, starttime, pgrp=pid)
    if leader_fd is None:
        return False
    fds = [leader_fd]
    try:
        confirmed = process_stat(pid)
        if not confirmed or confirmed["starttime"] != starttime or confirmed["pgrp"] != pid:
            return False
        for child, child_start, parent in collect_descendants(pid, starttime):
            child_fd = open_verified_pidfd(child, child_start, pgrp=pid, ppid=parent)
            if child_fd is not None:
                fds.append(child_fd)
        sent = False
        for pidfd in fds:
            try:
                signal.pidfd_send_signal(pidfd, sig)
                sent = True
            except OSError:
                continue
        return sent
    finally:
        for pidfd in fds:
            os.close(pidfd)


def signal_pipeline(pid, starttime, sig):
    ident = pipeline_identity(pid, starttime)
    if not ident:
        return False
    return signal_started_group(pid, ident["starttime"], sig)


def stop_pipeline():
    pid, starttime = read_pid_record()
    ident = pipeline_identity(pid, starttime)
    if ident:
        started = ident["starttime"]
        if signal_pipeline(pid, started, signal.SIGINT):
            for _ in range(40):
                if not pipeline_identity(pid, started):
                    break
                time.sleep(0.05)
            if pipeline_identity(pid, started):
                signal_pipeline(pid, started, signal.SIGTERM)
            for _ in range(20):
                if not pipeline_identity(pid, started):
                    break
                time.sleep(0.05)
            if pipeline_identity(pid, started):
                signal_pipeline(pid, started, signal.SIGKILL)
    clear_pid_record()
    try:
        os.remove(SOCK_PATH)
    except OSError:
        pass


def restart_pipeline(preview, record_path="", append=False):
    stop_pipeline()
    if not preview and not record_path:
        try:
            os.remove(CAPTURE_PATH)
        except OSError:
            pass
        return ""
    return start_pipeline(preview, record_path, append)


def set_sound(enabled):
    state = load_state()
    state["sound"] = bool(enabled)
    save_state(state)
    if preview_up():
        mpv_set_mute(not enabled)
    return ""


def set_live(enabled):
    recover_capture()
    recording = capture_target()
    if enabled:
        if preview_up():
            mpv_set_mute(not load_state().get("sound"))
            return ""
        # Keep writing the same file if a capture is already open.
        return restart_pipeline(True, recording, append=bool(recording))
    if recording and pipeline_running():
        # Closing the window must not cut the file.
        if preview_up():
            mpv_quit()
        return ""
    stop_pipeline()
    return ""


def set_recording(enabled):
    recover_capture()
    preview = preview_up() or (pipeline_running() and not capture_target())
    if enabled:
        if capture_target() and pipeline_running():
            return ""
        path = new_capture_path()
        # A preview-only pipeline has to be rebuilt so the file gets a copy.
        return restart_pipeline(True if preview or not pipeline_running() else False, path, append=False)
    target = capture_target()
    if not target:
        return ""
    state = load_state()
    state["lastCapture"] = target
    save_state(state)
    want_preview = preview_up()
    message = restart_pipeline(want_preview, "")
    try:
        os.remove(CAPTURE_PATH)
    except OSError:
        pass
    return message


def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    argument = argv[2] if len(argv) > 2 else ""
    message = ""
    code = 0
    if command == "status":
        pass
    elif command == "transport":
        return transport(argument)
    elif command == "live":
        current = preview_up()
        enabled = argument == "on" or (argument == "toggle" and not current)
        if argument == "off":
            enabled = False
        elif argument not in ("on", "off", "toggle"):
            message = "use live on, off, or toggle"
            code = 2
            enabled = current
        if code == 0:
            message = set_live(enabled)
            if message:
                code = 1
    elif command == "record":
        current = bool(capture_target()) and pipeline_running()
        enabled = argument == "on" or (argument == "toggle" and not current)
        if argument == "off":
            enabled = False
        elif argument not in ("on", "off", "toggle"):
            message = "use record on, off, or toggle"
            code = 2
        else:
            message = set_recording(enabled)
            if message:
                code = 1
    elif command == "sound":
        current = load_state().get("sound") is True
        enabled = argument == "on" or (argument == "toggle" and not current)
        if argument == "off":
            enabled = False
        elif argument not in ("on", "off", "toggle"):
            message = "use sound on, off, or toggle"
            code = 2
        else:
            message = set_sound(enabled)
    else:
        message = "use status, transport, live, record, or sound"
        code = 2
    emit(snapshot(message))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except BrokenPipeError:
        sys.exit(0)
    except Exception as error:
        emit(snapshot(str(error)))
        sys.exit(1)
