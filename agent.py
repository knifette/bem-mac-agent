#!/usr/bin/env python3
"""agent.py — Lux Remote Support persistent client agent (Windows).

The always-on companion a client installs once and leaves running. It:
  1) enrolls ONCE with an operator-issued code -> stores {agent_id, token},
  2) holds a persistent control WebSocket to Lux + heartbeats (so the client
     shows ONLINE in the operator console),
  3) on an operator "connect" command, spawns the WebRTC sender (rtc_sender.py)
     to share this screen for that session,
  4) shows the operator's chat messages.

A visible system-tray indicator + auto-start service are the INSTALLER slice;
this console build proves the unattended loop. Unattended is consented at enroll.

Enroll (one-time):  python agent.py --server wss://lux.bem.solutions --register <CODE> [--label "Name"]
Run (persistent):   python agent.py --server wss://lux.bem.solutions
Creds: %APPDATA%/BEMSupport/agent.json
(desktop-app lane, 2026-06-22. bridges/lux-observer is angelo-ops's; additive new file.)
"""
import argparse
import asyncio
import base64
import io
import json
import os
import platform
import subprocess
import sys

import requests
import websockets

# Image px scale factor from the last AI capture (screen_px / image_px), so the
# AI loop can send image-space coords and we inject at real screen coords.
_AI_SCALE = {"f": 1.0}

# ── Shared support engine versioning ─────────────────────────────────────────
# This file IS the canonical "support core": the standalone BEMSupport app runs
# it directly (via __main__), and the BEM desktop Helper (Aria) bundles the very
# same file as `support_core` and calls run(server, embed={...}) to expose its
# own "Get Help" button. The sync build step refuses to package either app unless
# both ship THIS version — so an update to the engine reaches BOTH installers.
SUPPORT_CORE_VERSION = "1.0.0"

# When the engine is embedded inside a host app (Aria), the host passes UI
# callbacks here instead of agent.py building its own window. Empty = standalone.
#   set_status(text) · on_connected(bool) · on_code(cc) · request_consent(allow, deny)
_EMBED = {}


async def _run_sender(server, sid, stok):
    """Stream this screen for a session — IN-PROCESS (no subprocess), so it works
    identically whether running as agent.py or the frozen BEMSupport.exe."""
    _RUN["in_session"] = True
    try:
        import types
        import rtc_sender
        await rtc_sender.run(types.SimpleNamespace(session=sid, token=stok,
                                                   server=server, mode="control", fps=15))
    except Exception as e:
        print("[BEM Support] sender error:", str(e)[:150])
    finally:
        _RUN["in_session"] = False
        _set_status("●  Waiting for a technician…")
        _set_connected(False)


async def _fs_list(ws, m):
    p = m.get("path") or os.path.expanduser("~")
    out = {"type": "fs_result", "req_id": m.get("req_id"), "path": os.path.abspath(p)}
    try:
        entries = []
        for name in sorted(os.listdir(p), key=str.lower):
            fp = os.path.join(p, name)
            try:
                isdir = os.path.isdir(fp)
                sz = None if isdir else os.path.getsize(fp)
            except Exception:
                isdir, sz = False, None
            entries.append({"name": name, "dir": isdir, "size": sz})
        out["entries"] = entries
    except Exception as e:
        out["error"] = str(e)[:150]
    await ws.send(json.dumps(out))


async def _fs_get(ws, m):
    p = m.get("path")
    out = {"type": "fs_result", "req_id": m.get("req_id")}
    try:
        if os.path.getsize(p) > 25 * 1024 * 1024:
            out["error"] = "file >25MB (v1 cap)"
        else:
            with open(p, "rb") as f:
                out["data"] = base64.b64encode(f.read()).decode()
            out["name"] = os.path.basename(p)
    except Exception as e:
        out["error"] = str(e)[:150]
    await ws.send(json.dumps(out))


async def _fs_put(ws, m):
    out = {"type": "fs_result", "req_id": m.get("req_id")}
    try:
        data = base64.b64decode(m.get("data") or "")
        if len(data) > 25 * 1024 * 1024:
            out["error"] = "file >25MB (v1 cap)"; await ws.send(json.dumps(out)); return
        folder = m.get("path") or os.path.expanduser("~")
        base = os.path.basename(m.get("name") or "upload.bin")   # strip any path / traversal
        dest = os.path.join(folder, base)
        if not os.path.realpath(dest).startswith(os.path.realpath(folder) + os.sep):
            out["error"] = "invalid destination"; await ws.send(json.dumps(out)); return
        with open(dest, "wb") as f:
            f.write(data)
        out["written"] = os.path.abspath(dest)
    except Exception as e:
        out["error"] = str(e)[:150]
    await ws.send(json.dumps(out))


def _sysinfo():
    """Read this PC's specs as plain text (stdlib only — bundles cleanly)."""
    import platform, shutil
    L = []
    try: L.append("Computer:  " + platform.node())
    except Exception: pass
    try: L.append("OS:        " + platform.system() + " " + platform.release() + " (build " + platform.version() + ")")
    except Exception: pass
    try: L.append("Arch:      " + platform.machine())
    except Exception: pass
    cpu = ""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip(); winreg.CloseKey(k)
    except Exception:
        cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "?")
    L.append("CPU:       " + cpu + "  (" + str(os.cpu_count()) + " logical cores)")
    try:
        import ctypes
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS(); m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        L.append("RAM:       %.1f GB total, %.1f GB free (%d%% used)" % (
            m.ullTotalPhys / 1e9, m.ullAvailPhys / 1e9, m.dwMemoryLoad))
    except Exception: pass
    try:
        du = shutil.disk_usage("C:\\")
        L.append("Disk C:    %.0f GB total, %.0f GB free" % (du.total / 1e9, du.free / 1e9))
    except Exception: pass
    return "\n".join(L) or "Could not read system info."


async def _run_cmd(ws, m):
    """Run a PowerShell/CMD command HEADLESS (no window, captured output) and
    return the text. Zero AI credits — the preferred path for anything scriptable
    (specs, DNS, services, disk, network) vs the vision loop."""
    import subprocess
    out = {"type": "cmd_result", "req_id": m.get("req_id")}
    shell = (m.get("shell") or "ps").lower()
    cmd = m.get("cmd") or ""

    def _run():
        if shell == "cmd":
            args = ["cmd", "/c", cmd]
        else:
            args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]
        # stdin=DEVNULL is REQUIRED: a --windowed (no-console) exe has no valid
        # stdin handle, so without this the child hangs forever. CREATE_NO_WINDOW
        # keeps anything from flashing on the client's screen.
        return subprocess.run(args, capture_output=True, text=True, timeout=90,
                              stdin=subprocess.DEVNULL, creationflags=0x08000000)
    try:
        p = await asyncio.to_thread(_run)   # off the event loop — heartbeats keep flowing
        txt = (p.stdout or "")
        if (p.stderr or "").strip():
            txt += ("\n[stderr]\n" + p.stderr)
        out["text"] = txt.strip() or "(no output)"
        out["code"] = p.returncode
    except subprocess.TimeoutExpired:
        out["error"] = "command timed out (90s)"
    except Exception as e:
        out["error"] = str(e)[:200]
    await ws.send(json.dumps(out))


def _is_admin_user():
    """True if the logged-in user is a member of local Administrators (even if the
    current process is not elevated) — i.e. elevation via a scheduled task is possible."""
    try:
        import subprocess
        r = subprocess.run(["whoami", "/groups"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL, creationflags=0x08000000)
        # S-1-5-32-544 = BUILTIN\Administrators; present + enabled when the user is an admin
        return "S-1-5-32-544" in (r.stdout or "")
    except Exception:
        return False


async def _elevated_run(ws, m):
    """Run a command ELEVATED (admin) via a one-shot HIGHEST-privilege scheduled task.
    Works with NO UAC prompt when the logged-in user is a local admin — the real
    IT-fix path (restart services, add printer ports, drivers). The OPERATOR side
    denylists the payload before it ever reaches here; this is an audited capability."""
    import subprocess, tempfile, uuid, os, time
    out = {"type": "cmd_result", "req_id": m.get("req_id")}
    shell = (m.get("shell") or "ps").lower()
    cmd = m.get("cmd") or ""
    if not _is_admin_user():
        out["error"] = "this PC's user is not a local administrator — cannot elevate"
        await ws.send(json.dumps(out)); return
    tid = "BEMElev_" + uuid.uuid4().hex[:8]
    tmp = tempfile.gettempdir()
    out_file = os.path.join(tmp, tid + ".out")

    def _work():
        if shell == "cmd":
            script = os.path.join(tmp, tid + ".bat")
            body = f"@echo off\r\n(\r\n{cmd}\r\n) > \"{out_file}\" 2>&1\r\n"
            tr = f'"{script}"'
        else:
            script = os.path.join(tmp, tid + ".ps1")
            body = f"& {{\n{cmd}\n}} *>&1 | Out-File -FilePath '{out_file}' -Encoding utf8\n"
            tr = f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{script}"'
        try:
            with open(script, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            return None, f"write failed: {e}"
        cr = subprocess.run(["schtasks", "/create", "/tn", tid, "/tr", tr, "/sc", "once",
                             "/st", "23:59", "/rl", "highest", "/f"],
                            capture_output=True, text=True, timeout=30,
                            stdin=subprocess.DEVNULL, creationflags=0x08000000)
        if cr.returncode != 0:
            return None, f"elevate-create failed: {(cr.stderr or cr.stdout or '').strip()[:200]}"
        subprocess.run(["schtasks", "/run", "/tn", tid], capture_output=True, text=True,
                       timeout=30, stdin=subprocess.DEVNULL, creationflags=0x08000000)
        for _ in range(90):                       # wait for the task to finish (~90s cap)
            time.sleep(1)
            q = subprocess.run(["schtasks", "/query", "/tn", tid, "/fo", "list"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000)
            if "Running" not in (q.stdout or ""):
                break
        text = ""
        try:
            with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            text = ""
        try:
            subprocess.run(["schtasks", "/delete", "/tn", tid, "/f"], capture_output=True,
                           timeout=15, stdin=subprocess.DEVNULL, creationflags=0x08000000)
        except Exception:
            pass
        for p in (script, out_file):
            try: os.remove(p)
            except Exception: pass
        return text, None

    try:
        text, err = await asyncio.to_thread(_work)
        if err:
            out["error"] = err
        else:
            out["text"] = (text or "").strip() or "(no output)"
            out["code"] = 0
            out["elevated"] = True
    except Exception as e:
        out["error"] = str(e)[:200]
    await ws.send(json.dumps(out))


async def _ai_capture(ws, req_id):
    """Grab the screen (mss, independent of the streaming dxcam), downscale, and
    send a JPEG back so Lux's vision model can decide the next action."""
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        W = 1280
        f = img.width / W
        small = img.resize((W, int(img.height / f)))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=60)
        _AI_SCALE["f"] = f
        await ws.send(json.dumps({"type": "ai_frame", "req_id": req_id,
                                  "img": base64.b64encode(buf.getvalue()).decode(),
                                  "iw": small.width, "ih": small.height}))
    except Exception as e:
        await ws.send(json.dumps({"type": "ai_frame", "req_id": req_id, "error": str(e)[:120]}))


def _uia_click(target, double=False):
    """UI-Automation grounding: click the control whose visible Name matches
    `target` (exact, then contains) — far more reliable than raw model pixels.
    Returns True if it found + clicked an element."""
    if not target:
        return False
    try:
        import uiautomation as auto
        for kw in ({"Name": target}, {"SubName": target}):
            ctrl = auto.Control(searchDepth=0xFFFFFFFF, **kw)
            if ctrl.Exists(0.8, 0.1):
                (ctrl.DoubleClick if double else ctrl.Click)(simulateMove=False, waitTime=0.05)
                return True
    except Exception as e:
        print("[BEM Support] uia error:", str(e)[:120])
    return False


def _ai_inject(action):
    """Inject an AI-decided action onto this screen. Clicks prefer UIA grounding
    (by control label); everything falls back to rtc_sender's Win32 injector
    (which takes 0..1e4 normalized coords)."""
    try:
        from rtc_sender import _inject as inj, SCREEN_W, SCREEN_H
        kind = (action.get("action") or "").lower()
        f = _AI_SCALE["f"]
        sx, sy = (action.get("x") or 0) * f, (action.get("y") or 0) * f
        nx, ny = int(sx / SCREEN_W * 10000), int(sy / SCREEN_H * 10000)
        if kind in ("click", "double_click"):
            if not _uia_click(action.get("target"), double=(kind == "double_click")):
                inj({"kind": "mouse", "sub": "move", "x": nx, "y": ny})
                inj({"kind": "mouse", "sub": "down", "button": 0})
                inj({"kind": "mouse", "sub": "up", "button": 0})
                if kind == "double_click":
                    inj({"kind": "mouse", "sub": "down", "button": 0})
                    inj({"kind": "mouse", "sub": "up", "button": 0})
        elif kind == "type":
            for ch in (action.get("text") or ""):
                inj({"kind": "keyboard", "key": ch})
        elif kind == "key":
            inj({"kind": "keyboard", "key": (action.get("keys") or "").strip("{}") or "Enter"})
    except Exception as e:
        print("[BEM Support] inject error:", str(e)[:120])

CREDS = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                     "BEMSupport", "agent.json")
CONFIG = os.path.join(os.path.dirname(CREDS), "config.json")  # mass-deploy: {server, group}
HERE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Optional config dropped by the mass-deploy script (server + group)."""
    try:
        with open(CONFIG) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _machine_id():
    """Stable per-PC id so re-registers (New code / auto-recovery) reuse ONE
    server record instead of duplicating. Windows MachineGuid, MAC fallback."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        g = winreg.QueryValueEx(k, "MachineGuid")[0]; winreg.CloseKey(k)
        if g:
            return "mg:" + g
    except Exception:
        pass
    try:
        import uuid
        return "mac:" + format(uuid.getnode(), "012x")
    except Exception:
        return ""


def _exe_hash(path):
    # (auto-update self-test marker — bumping this changes the binary hash)
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


_INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BEMSupport")


def _is_onedir():
    """True for the --onedir build (a sibling _internal folder, no temp extraction)."""
    try:
        return getattr(sys, "frozen", False) and os.path.isdir(
            os.path.join(os.path.dirname(sys.executable), "_internal"))
    except Exception:
        return False


def _onedir_install_and_relaunch():
    """--onedir first run: if we're running from the unzipped download (not the install
    dir), copy the whole app folder to %LOCALAPPDATA%\\BEMSupport, point autostart there,
    relaunch from there, and tell the caller to exit. No temp extraction ever again —
    the fix for AV quarantine + slow load + 'failed to load Python'. Idempotent."""
    if not _is_onedir():
        return False
    src = os.path.dirname(sys.executable)
    if os.path.normcase(src.rstrip("\\/")) == os.path.normcase(_INSTALL_DIR.rstrip("\\/")):
        return False                                  # already running from the install dir
    try:
        import shutil, subprocess
        shutil.copytree(src, _INSTALL_DIR, dirs_exist_ok=True)
        exe = os.path.join(_INSTALL_DIR, "BEMSupport.exe")
        # NOTE: do NOT enable autostart here — persistence is OPT-IN (the client turns
        # on Auto-support via the button / minimize / Settings). Just run from the
        # installed copy so the existing Run-key (if any) points at the right exe.
        subprocess.Popen([exe], creationflags=0x08000000, close_fds=True)
        print("[BEM Support] installed to", _INSTALL_DIR)
        return True
    except Exception as e:
        print("[BEM Support] install skipped:", str(e)[:120])
        return False


def _self_update_onedir(server):
    """--onedir auto-update: compare the published ZIP hash to this install's VERSION;
    if newer, download the zip, extract, and a detached script swaps the whole
    %LOCALAPPDATA%\\BEMSupport folder + relaunches (after we exit so the files unlock).
    A fresh install with no VERSION just records the baseline (no spurious update).
    Returns True if an update was launched (caller should exit)."""
    try:
        info = requests.get(_http(server) + "/download/version", timeout=15).json()
        srv = info.get("zip_hash")
        if not srv:
            return False
        vfile = os.path.join(_INSTALL_DIR, "VERSION")
        try:
            with open(vfile) as f:
                cur = f.read().strip()
        except Exception:
            cur = None
        if cur is None:                       # fresh install — set baseline, don't update yet
            try:
                with open(vfile, "w") as f:
                    f.write(srv)
            except Exception:
                pass
            return False
        if cur == srv:
            return False
        import tempfile, zipfile, shutil, subprocess
        newroot = _INSTALL_DIR + ".new"
        tmpx = _INSTALL_DIR + ".x"
        for d in (newroot, tmpx):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        zpath = os.path.join(tempfile.gettempdir(), "BEMSupport_update.zip")
        with requests.get(_http(server) + "/download/bemsupport.zip", stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmpx)
        try:
            os.remove(zpath)
        except Exception:
            pass
        inner = os.path.join(tmpx, "BEMSupport")     # the zip wraps a top-level BEMSupport/
        appsrc = inner if os.path.isfile(os.path.join(inner, "BEMSupport.exe")) else tmpx
        if not os.path.isfile(os.path.join(appsrc, "BEMSupport.exe")):
            shutil.rmtree(tmpx, ignore_errors=True)
            return False
        shutil.move(appsrc, newroot)
        shutil.rmtree(tmpx, ignore_errors=True)
        with open(os.path.join(newroot, "VERSION"), "w") as f:
            f.write(srv)
        instexe = os.path.join(_INSTALL_DIR, "BEMSupport.exe")
        old = _INSTALL_DIR + ".old"
        ps = ("$ErrorActionPreference='SilentlyContinue'; "
              f"Wait-Process -Id {os.getpid()}; Start-Sleep -Milliseconds 1500; "
              f"Remove-Item -LiteralPath '{old}' -Recurse -Force; "
              f"if(Move-Item -LiteralPath '{_INSTALL_DIR}' -Destination '{old}' -Force -PassThru){{ "
              f"  if(Move-Item -LiteralPath '{newroot}' -Destination '{_INSTALL_DIR}' -Force -PassThru){{ "
              f"    Remove-Item -LiteralPath '{old}' -Recurse -Force "
              f"  }} else {{ Move-Item -LiteralPath '{old}' -Destination '{_INSTALL_DIR}' -Force }} "
              f"}}; Start-Process -FilePath '{instexe}'")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x08000000, close_fds=True)
        print("[BEM Support] updating to the latest version (onedir)…")
        return True
    except Exception as e:
        print("[BEM Support] onedir update skipped:", str(e)[:120])
        return False


def _self_update(server):
    """If the published exe differs from this one, download it + spawn an updater
    that swaps the binary and relaunches. Returns True if an update was launched
    (the caller should then exit). Only the frozen --onefile .exe self-updates."""
    if not getattr(sys, "frozen", False):
        return False
    if _is_onedir():
        return _self_update_onedir(server)   # folder-based; never pulls the onefile
    try:
        cur = sys.executable
        info = requests.get(_http(server) + "/download/version", timeout=15).json()
        srv = info.get("hash")
        if not srv or srv == _exe_hash(cur):
            return False
        newexe = cur + ".new"
        # Anti-flap: if a prior swap already downloaded THIS target but couldn't
        # copy it over the locked/protected exe, don't re-download every 30 min —
        # bail until a different version is published (or the .new is cleared).
        if os.path.exists(newexe) and _exe_hash(newexe) == srv:
            return False
        with requests.get(_http(server) + "/download/bemsupport", stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(newexe, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        if _exe_hash(newexe) != srv:          # corrupt/partial download — abort
            try:
                os.remove(newexe)
            except Exception:
                pass
            return False
        # Updater: wait for THIS process to exit, swap the exe (retry past the file
        # lock), relaunch. Only delete .new + launch the NEW exe on a SUCCESSFUL
        # copy; on failure relaunch the OLD exe (client stays alive) and keep .new
        # so the anti-flap check above stops the loop.
        import subprocess
        ps = (f"$ErrorActionPreference='SilentlyContinue'; Wait-Process -Id {os.getpid()}; "
              f"Start-Sleep -Milliseconds 900; $ok=$false; "
              f"for($i=0;$i -lt 12;$i++){{ Copy-Item -LiteralPath '{newexe}' -Destination '{cur}' -Force; "
              f"if($?){{ $ok=$true; break }}; Start-Sleep -Milliseconds 500 }}; "
              f"if($ok){{ Remove-Item -LiteralPath '{newexe}' -Force }}; Start-Process -FilePath '{cur}'")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x08000000, close_fds=True)
        print("[BEM Support] updating to the latest version…")
        return True
    except Exception as e:
        print("[BEM Support] update check skipped:", str(e)[:100])
        return False


def _http(server):
    return server.replace("wss://", "https://").replace("ws://", "http://")


def _load():
    try:
        with open(CREDS) as f:
            return json.load(f)
    except Exception:
        return None


def _save(d):
    os.makedirs(os.path.dirname(CREDS), exist_ok=True)
    with open(CREDS, "w") as f:
        json.dump(d, f)


def _remember_consent():
    """Persist 'always allow BEM' so the client is asked ONLY on the first connect."""
    try:
        d = _load() or {}
        d["always_allow"] = True
        _save(d)
    except Exception:
        pass


def register(server, code, label, personal_key=None, group=None):
    payload = {"client_label": label or platform.node(), "host": platform.node(),
               "machine_id": _machine_id()}
    if code:
        payload["enroll_code"] = code
    if personal_key:
        payload["personal_key"] = personal_key
    if group:
        payload["group"] = group
    r = requests.post(_http(server) + "/api/support/agents/register", json=payload, timeout=15)
    if r.status_code != 200:
        print("Registration failed:", r.status_code, r.text[:200]); return None
    d = r.json()
    _save({"agent_id": d["agent_id"], "token": d["agent_token"],
           "connect_code": d.get("connect_code"), "server": server})
    return d.get("connect_code")


_GUI = {"root": None, "status": None}
_TRAY = {"icon": None}  # system-tray icon (created lazily when the window is closed/minimized)
import queue as _queue
_TRAY_Q = _queue.Queue()  # tray clicks run on the pystray thread; enqueue → GUI thread drains (tkinter isn't thread-safe)
_ALWAYS_ALLOW = False   # set from saved creds on run(); True after the first Allow
_RUN = {"aid": None, "tok": None, "server": None, "ws": None}  # live connection state


def _set_status(text):
    cb = _EMBED.get("set_status")
    if cb:
        try: cb(text)
        except Exception: pass
        return
    try:
        if _GUI["status"] is not None:
            _GUI["status"].set(text)
    except Exception:
        pass


def _copy_code():
    try:
        r = _GUI.get("root")
        if r is not None and _GUI.get("code"):
            r.clipboard_clear(); r.clipboard_append(_GUI["code"]); r.update()
    except Exception:
        pass


def _update_code(cc):
    """Refresh the on-screen code after a re-registration (auto-recovery)."""
    cb = _EMBED.get("on_code")
    if cb:
        try: cb(cc)
        except Exception: pass
        return
    try:
        disp = cc if (cc and "-" in cc) else \
            (f"{cc[:3]}-{cc[3:]}" if (cc and len(cc) >= 6) else (cc or "—"))
        if _GUI.get("codevar") is not None:
            _GUI["codevar"].set(disp)
        _GUI["code"] = (cc or "").replace("-", "")
    except Exception:
        pass


def _new_code():
    """Client-clicked 'New code' — re-register fresh and move the live connection
    onto the new creds. Always works, even if the old code was removed/stale."""
    try:
        _set_status("●  Getting a new code…")
        srv = _RUN.get("server") or "wss://help.bem.solutions"
        cc = register(srv, None, None, None, _load_config().get("group"))
        c2 = _load()
        if c2:
            _RUN["aid"], _RUN["tok"] = c2["agent_id"], c2["token"]
            if cc:
                _update_code(cc)
        ws = _RUN.get("ws")           # drop the old link so run() reconnects fresh
        if ws is not None:
            try:
                asyncio.ensure_future(ws.close())
            except Exception:
                pass
        _set_status("●  Waiting for a technician…")
    except Exception:
        _set_status("●  Could not refresh — check the internet.")


def _set_connected(on):
    """Show/hide the persistent red 'being viewed' banner + raise the window."""
    cb = _EMBED.get("on_connected")
    if cb:
        try: cb(bool(on))
        except Exception: pass
        return
    try:
        b = _GUI.get("banner")
        if b is None:
            return
        if on:
            b.pack(side="top", fill="x", before=_GUI.get("title"))
            r = _GUI.get("root")
            if r is not None:
                try:
                    r.deiconify(); r.attributes("-topmost", True)
                    r.after(1200, lambda: r.attributes("-topmost", False))
                except Exception:
                    pass
        else:
            b.pack_forget()
    except Exception:
        pass


def _request_consent(on_allow, on_deny=None):
    """Non-blocking Allow/Deny dialog before a tech can view the screen — shown
    ONLY on the first connect; after the client clicks Allow once we remember it
    ('always allow BEM') and connect straight through every time after.
    Headless (no GUI window) -> the spoken code itself is the consent."""
    cb = _EMBED.get("request_consent")
    if cb:                                  # host (Aria) owns the consent prompt
        try: cb(lambda: (_set_connected(True), on_allow()), on_deny)
        except Exception: (_set_connected(True), on_allow())
        return
    if _EMBED:                              # embedded, no custom prompt: clicking
        _set_connected(True); on_allow(); return   # "Get Help" already IS consent
    if _ALWAYS_ALLOW:                       # already consented once -> frictionless
        _set_connected(True); on_allow(); return
    try:
        import tkinter as tk
    except Exception:
        on_allow(); return
    r = _GUI.get("root")
    if r is None:
        on_allow(); return
    try:
        d = tk.Toplevel(r); d.title("BEM Remote Support")
        d.configure(bg="#0b0f0d"); d.geometry("410x256"); d.resizable(False, False)
        try:
            d.attributes("-topmost", True); d.grab_set()
        except Exception:
            pass
        tk.Label(d, text="Allow BEM Support?", bg="#0b0f0d", fg="#eaf2ee",
                 font=("Segoe UI", 15, "bold")).pack(pady=(26, 6))
        tk.Label(d, text="BEM is your IT support. Allow them to view and\n"
                 "control this PC to help you when needed.\nYou won't be asked again.",
                 bg="#0b0f0d", fg="#9fb8ac", font=("Segoe UI", 10), justify="center").pack()
        row = tk.Frame(d, bg="#0b0f0d"); row.pack(pady=22)

        def _allow():
            try:
                d.destroy()
            except Exception:
                pass
            global _ALWAYS_ALLOW
            _ALWAYS_ALLOW = True
            _remember_consent()             # persist so future connects skip this
            _set_connected(True); on_allow()

        def _deny():
            try:
                d.destroy()
            except Exception:
                pass
            _set_status("●  You declined — no one can see your screen")
            if on_deny:
                on_deny()
        tk.Button(row, text="Allow", command=_allow, bg="#0A6E50", fg="#ffffff",
                  activebackground="#0d8a64", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=28, pady=9, cursor="hand2").pack(side="left", padx=8)
        tk.Button(row, text="Deny", command=_deny, bg="#241414", fg="#ffb4b4",
                  activebackground="#3a1d1d", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=28, pady=9, cursor="hand2").pack(side="left", padx=8)
        d.protocol("WM_DELETE_WINDOW", _deny)
    except Exception:
        on_allow()


def _remove_autostart():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, "BEMSupport"); winreg.CloseKey(k)
    except Exception:
        pass


def _autostart_on():
    """True if BEM is set to start with Windows — the Run key OR the elevated logon
    task (admin mode). Drives the 'Auto-support' toggle state."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(k, "BEMSupport"); winreg.CloseKey(k); return True
        except FileNotFoundError:
            winreg.CloseKey(k)
    except Exception:
        pass
    try:
        return _elevated_task_exists()
    except Exception:
        return False


def _submit_ticket(text, kind="help"):
    """Send a help ticket OR a plain message over the live control WS (thread-safe).
    kind='help' → Lux triages it; kind='message' → just a note to the operator."""
    ws = _RUN.get("ws"); loop = _RUN.get("loop")
    if ws is None or loop is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps({"type": "ticket", "text": (text or "")[:2000], "kind": kind})), loop)
        return True
    except Exception:
        return False


def _open_help(mode="help"):
    """Native window. mode='help' → describe a problem (Lux triages + can fix).
    mode='message' → send a quick note to your technician."""
    import tkinter as tk
    root = _GUI.get("root")
    if root is None:
        return
    is_msg = (mode == "message")
    try:
        w = tk.Toplevel(root)
        w.title(("Message BEM Support" if is_msg else "Get Help") + " — BEM Remote Support")
        w.configure(bg="#0b0f0d"); w.geometry("460x360"); w.resizable(False, False)
        tk.Label(w, text=("Message your technician" if is_msg else "Tell us what's wrong"),
                 bg="#0b0f0d", fg="#27c08c", font=("Segoe UI", 15, "bold")).pack(pady=(18, 2))
        tk.Label(w, text=("Send a quick note to BEM — a question, scheduling,\nor anything else. It lands in their inbox."
                          if is_msg else
                          "Describe the problem in your own words. BEM Support gets a\n"
                          "ticket right away — Lux may even be able to fix it for you."),
                 bg="#0b0f0d", fg="#82978c", font=("Segoe UI", 9), justify="center").pack()
        txt = tk.Text(w, height=6, width=48, bg="#111a16", fg="#eaf2ee", insertbackground="#eaf2ee",
                      relief="flat", font=("Segoe UI", 10), wrap="word", padx=10, pady=8)
        txt.pack(padx=22, pady=14); txt.focus_set()
        msg = tk.StringVar(value="")
        tk.Label(w, textvariable=msg, bg="#0b0f0d", fg="#9fd9c4", font=("Segoe UI", 9)).pack()
        def _send():
            body = txt.get("1.0", "end").strip()
            if not body:
                msg.set("Type your message first." if is_msg else "Please describe the problem first."); return
            if _submit_ticket(body, kind=("message" if is_msg else "help")):
                msg.set("✓  Sent to BEM Support — we'll take it from here.")
                try: w.after(1700, w.destroy)
                except Exception: pass
            else:
                msg.set("Couldn't send right now — make sure you're online.")
        tk.Button(w, text=("Send message" if is_msg else "Send to BEM Support"), command=_send,
                  bg="#0A6E50", fg="#ffffff", activebackground="#0c8a64", activeforeground="#fff",
                  relief="flat", font=("Segoe UI", 11, "bold"), padx=18, pady=9, cursor="hand2").pack(pady=8)
        try:
            w.attributes("-topmost", True)
            w.after(1200, lambda: w.attributes("-topmost", False))
        except Exception:
            pass
    except Exception:
        pass


def _build_gui(code):
    """A small branded window the client sees: their code + status + Stop.
    Returns True if a window was created (else the agent runs headless)."""
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        root = tk.Tk()
        root.title("BEM Remote Support")
        root.configure(bg="#0b0f0d")
        root.geometry("470x582"); root.resizable(False, False)

        def _stop():
            _remove_autostart()
            try:
                ic = _TRAY.get("icon")
                if ic is not None:
                    ic.stop()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
            os._exit(0)

        def _ensure_tray():
            # Build the system-tray icon once; show/quit from its menu. Tray runs in
            # its own thread (run_detached) and talks back via root.after (thread-safe).
            if _TRAY.get("icon") is not None:
                return
            try:
                import pystray
                from PIL import Image, ImageDraw
            except Exception:
                try: root.iconify()   # no tray lib → at least minimize, never kill the link
                except Exception: pass
                return
            img = Image.new("RGB", (64, 64), "#0b0f0d")
            ImageDraw.Draw(img).ellipse((14, 14, 50, 50), fill="#27c08c")
            # Tray clicks run on the pystray thread → just ENQUEUE; the GUI loop (_pump)
            # drains the queue and runs the action on the tkinter thread (thread-safe).
            menu = pystray.Menu(
                pystray.MenuItem("Get Help", lambda i, it: _TRAY_Q.put("help")),
                pystray.MenuItem("Message BEM", lambda i, it: _TRAY_Q.put("message")),
                pystray.MenuItem("Show BEM Remote Support", lambda i, it: _TRAY_Q.put("show"), default=True),
                pystray.MenuItem("Settings", lambda i, it: _TRAY_Q.put("settings")),
                pystray.MenuItem("Stop sharing", lambda i, it: _TRAY_Q.put("stop")))
            ic = pystray.Icon("bem_support", img, "BEM Remote Support", menu)
            _TRAY["icon"] = ic
            ic.run_detached()

        def _hide_to_tray():
            # X / minimize → tray + turn ON Auto-support (keep running across reboots).
            # Only "Stop sharing" fully disconnects (and removes from startup).
            try: _autostart()
            except Exception: pass
            try: root.withdraw()
            except Exception: pass
            _ensure_tray()
            try: _refresh_auto()
            except Exception: pass

        def _tray_show():
            try:
                root.deiconify(); root.lift()
                root.attributes("-topmost", True)
                root.after(700, lambda: root.attributes("-topmost", False))
            except Exception:
                pass
        # Action table the GUI loop runs when a tray item is clicked (see _pump).
        _GUI["actions"] = {
            "show": _tray_show,
            "help": lambda: _open_help(),
            "message": lambda: _open_help("message"),
            "settings": lambda: (_GUI.get("open_settings") or (lambda: None))(),
            "stop": _stop,
        }

        try:
            root.attributes("-topmost", True)
            root.after(1400, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
        # persistent red "being viewed" banner — packed only while a session is live
        banner = tk.Label(root, text="●  Your screen is being viewed by BEM Support",
                          bg="#7a1f1f", fg="#ffffff", font=("Segoe UI", 10, "bold"), pady=7)
        title = tk.Label(root, text="BEM Remote Support", bg="#0b0f0d", fg="#27c08c",
                         font=("Segoe UI", 17, "bold"))
        title.pack(pady=(22, 2))
        tk.Label(root, text="Read this code to your technician", bg="#0b0f0d", fg="#82978c",
                 font=("Segoe UI", 10)).pack()
        disp = code if (code and "-" in code) else \
            (f"{code[:3]}-{code[3:]}" if (code and len(code) >= 6) else (code or "—"))
        codevar = tk.StringVar(value=disp)
        tk.Label(root, textvariable=codevar, bg="#0b0f0d", fg="#eaf2ee",
                 font=("Consolas", 42, "bold")).pack(pady=(14, 6))
        crow = tk.Frame(root, bg="#0b0f0d")
        tk.Button(crow, text="Copy code", command=_copy_code, bg="#15201b", fg="#cfe6db",
                  activebackground="#1d2c25", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=14, pady=5, cursor="hand2").pack(side="left", padx=4)
        tk.Button(crow, text="New code", command=_new_code, bg="#15201b", fg="#cfe6db",
                  activebackground="#1d2c25", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=14, pady=5, cursor="hand2").pack(side="left", padx=4)
        crow.pack(pady=(0, 14))
        grow = tk.Frame(root, bg="#0b0f0d")
        tk.Button(grow, text="Get Help", command=_open_help, bg="#0A6E50", fg="#ffffff",
                  activebackground="#0c8a64", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=22, pady=9, cursor="hand2").pack(side="left", padx=4)
        tk.Button(grow, text="💬 Message", command=lambda: _open_help("message"), bg="#15201b", fg="#cfe6db",
                  activebackground="#1d2c25", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=18, pady=9, cursor="hand2").pack(side="left", padx=4)
        grow.pack(pady=(0, 8))
        # ── Auto-support: opt-in persistence (start with Windows + auto-reconnect) ──
        asv = tk.StringVar()

        def _refresh_auto():
            on = _autostart_on()
            asv.set("✓  Auto-support is ON — starts with your PC" if on
                    else "Turn on Auto-support  (start with my PC)")
            try:
                autob.configure(bg="#103a2a" if on else "#15201b", fg="#27c08c" if on else "#cfe6db")
            except Exception:
                pass

        def _toggle_auto():
            try:
                _remove_autostart() if _autostart_on() else _autostart()
            except Exception:
                pass
            _refresh_auto()

        autob = tk.Button(root, textvariable=asv, command=_toggle_auto, relief="flat",
                          activebackground="#1d2c25", activeforeground="#fff",
                          font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2")
        autob.pack(pady=(0, 6))
        _refresh_auto()

        def _open_settings():
            import tkinter as _tk
            w = _tk.Toplevel(root); w.title("BEM Support — Settings")
            w.configure(bg="#0b0f0d"); w.geometry("400x240"); w.resizable(False, False)
            _tk.Label(w, text="Settings", bg="#0b0f0d", fg="#27c08c",
                      font=("Segoe UI", 14, "bold")).pack(pady=(18, 8))
            ss = _tk.StringVar()
            def _sr():
                ss.set("Auto-support: ON  (starts with your PC)" if _autostart_on() else "Auto-support: OFF")
            _tk.Label(w, textvariable=ss, bg="#0b0f0d", fg="#cfe6db", font=("Segoe UI", 11)).pack(pady=6)
            def _st():
                _toggle_auto(); _sr()
            _tk.Button(w, text="Toggle Auto-support", command=_st, bg="#15201b", fg="#cfe6db",
                       relief="flat", font=("Segoe UI", 10, "bold"), padx=16, pady=8,
                       cursor="hand2").pack(pady=8)
            _tk.Label(w, text="Auto-support keeps BEM running so your technician can help\n"
                      "without you reopening it. Turn it off here anytime.",
                      bg="#0b0f0d", fg="#82978c", font=("Segoe UI", 9), justify="center").pack(pady=10)
            _sr()
            try:
                w.attributes("-topmost", True); w.after(1200, lambda: w.attributes("-topmost", False))
            except Exception:
                pass
        _GUI["open_settings"] = _open_settings
        _GUI["refresh_auto"] = _refresh_auto
        sv = tk.StringVar(value="●  Waiting for a technician…")
        tk.Label(root, textvariable=sv, bg="#111a16", fg="#9fb8ac",
                 font=("Segoe UI", 11), padx=14, pady=11).pack(fill="x", padx=28)
        tk.Label(root, text="A technician sees/controls your screen ONLY while connected —\n"
                 "you'll see a red banner the whole time.\n"
                 "Closing (X) tucks BEM into the tray and keeps it on (Auto-support).\n"
                 "Stop sharing fully quits and removes it from startup.",
                 bg="#0b0f0d", fg="#82978c", font=("Segoe UI", 9), justify="center").pack(pady=12)
        tk.Button(root, text="Stop sharing", command=_stop, bg="#3a1d1d", fg="#ffb4b4",
                  activebackground="#4a2424", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=18, pady=8, cursor="hand2").pack()
        tk.Button(root, text="⚙  Settings", command=_open_settings, bg="#111a16", fg="#9fb8ac",
                  activebackground="#1d2c25", activeforeground="#fff", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=16, pady=6, cursor="hand2").pack(pady=(10, 4))
        root.protocol("WM_DELETE_WINDOW", _hide_to_tray)   # X → tray + Auto-support on
        _GUI.update({"root": root, "status": sv, "banner": banner, "title": title,
                     "codevar": codevar, "code": (code or "").replace("-", "")})
        return True
    except Exception:
        return False


async def run(server, embed=None):
    """The support engine loop: enroll-check → heartbeat → WS dispatch → fixes.
    embed=None → standalone app (builds its own branded window).
    embed={callbacks} → hosted inside Aria: skip our window/tray/self-updater and
    route status/consent/code through the host's UI."""
    global _EMBED
    _EMBED = embed or {}
    creds = _load()
    if not creds:
        print("Not set up yet — run the installer again."); return
    aid, tok = creds["agent_id"], creds["token"]
    cc = creds.get("connect_code")
    global _ALWAYS_ALLOW
    _ALWAYS_ALLOW = bool(creds.get("always_allow"))   # remembered first-connect consent
    _RUN.update({"aid": aid, "tok": tok, "server": server})
    try: _RUN["loop"] = asyncio.get_running_loop()   # for thread-safe ticket sends from the GUI
    except Exception: _RUN["loop"] = None
    _RUN["elevated"] = _is_elevated()           # cached once — used by heartbeat + setup_elevated
    _RUN["admin_user"] = _is_admin_user()
    if _RUN["elevated"] and not _elevated_task_exists():
        _install_elevated_task()                # self-heal: any elevated launch ensures admin-mode persists
    print("=" * 54)
    print("   BEM Remote Support")
    if cc:
        print("")
        print(f"      YOUR CODE:   {cc}")
        print("")
        print("   Read this code to your technician so they can connect.")
    print("   Keep this window open — you'll see a banner when connected.")
    print("=" * 54)
    sys.stdout.flush()   # ensure the client sees their code immediately
    if not _EMBED:
        _build_gui(cc)   # branded window: code + status + Stop (headless if no GUI)

    async def _pump():   # drive tkinter from the asyncio loop (same thread, safe)
        r = _GUI.get("root")
        if r is None:
            return
        while True:
            try:
                r.update()
            except Exception:
                return
            # run any tray-menu actions (enqueued from the pystray thread) HERE on the
            # GUI thread — tkinter calls from another thread silently no-op.
            acts = _GUI.get("actions") or {}
            while True:
                try:
                    name = _TRAY_Q.get_nowait()
                except _queue.Empty:
                    break
                try:
                    fn = acts.get(name)
                    if fn:
                        fn()
                except Exception:
                    pass
            await asyncio.sleep(0.04)
    if not _EMBED:
        asyncio.ensure_future(_pump())   # standalone owns the tkinter pump

    async def _upd():   # check for a newer published exe periodically + self-update
        while True:
            await asyncio.sleep(1800)
            if _RUN.get("in_session"):     # never interrupt a live support call
                continue
            try:
                if await asyncio.to_thread(_self_update, server):
                    os._exit(0)
            except Exception:
                pass
    if not _EMBED:
        asyncio.ensure_future(_upd())     # Aria runs its OWN updater when embedded

    while True:
        url = f"{_RUN['server']}/api/support/agent/{_RUN['aid']}/control?token={_RUN['tok']}"
        try:
            async with websockets.connect(url, max_size=2 ** 20) as ws:
                _RUN["ws"] = ws
                print("[BEM Support] online — waiting for your technician.")
                _set_status("●  Waiting for a technician…")

                async def beat():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send(json.dumps({"type": "heartbeat",
                                                      "elevated": _RUN.get("elevated"),
                                                      "admin_user": _RUN.get("admin_user")}))
                        except Exception:
                            return
                bt = asyncio.ensure_future(beat())
                try:
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        t = m.get("type")
                        if t == "connect":
                            sid, stok = m.get("session_id"), m.get("token")
                            print(f"[BEM Support] connect request — session {sid}.")
                            _set_status("●  A technician is connecting…")

                            def _go(server=server, sid=sid, stok=stok):
                                print(f"[BEM Support] *** CONNECTED — session {sid}. ***")
                                _set_status("●  Connected — a technician is helping you")
                                asyncio.ensure_future(_run_sender(server, sid, stok))
                            _request_consent(_go)
                        elif t == "chat":
                            print(f"[BEM Support — operator] {m.get('text')}")
                        elif t == "ai_capture":
                            await _ai_capture(ws, m.get("req_id"))
                        elif t == "ai_action":
                            _ai_inject(m.get("action") or {})
                        elif t == "fs_list":
                            await _fs_list(ws, m)
                        elif t == "fs_get":
                            await _fs_get(ws, m)
                        elif t == "fs_put":
                            await _fs_put(ws, m)
                        elif t == "sysinfo":
                            await ws.send(json.dumps({"type": "sysinfo_result",
                                                      "req_id": m.get("req_id"), "text": _sysinfo()}))
                        elif t == "run_cmd":
                            await _run_cmd(ws, m)
                        elif t == "elevated_run":
                            await _elevated_run(ws, m)
                        elif t == "setup_elevated":
                            rid = m.get("req_id")
                            if _is_elevated():
                                ok = _elevated_task_exists() or _install_elevated_task()
                                await ws.send(json.dumps({"type": "cmd_result", "req_id": rid, "elevated": True,
                                    "text": "Admin mode is active — this PC runs elevated at every logon now." if ok
                                            else "Running elevated, but the logon task could not be saved."}))
                            elif _is_admin_user():
                                _elevate_self()
                                await ws.send(json.dumps({"type": "cmd_result", "req_id": rid,
                                    "text": "A Windows permission prompt is showing on the PC — click YES once to turn on admin mode (one time only)."}))
                            else:
                                await ws.send(json.dumps({"type": "cmd_result", "req_id": rid,
                                    "error": "this PC's user is not a local administrator — admin mode needs an admin account"}))
                        elif t == "quality":
                            try:
                                import rtc_sender
                                rtc_sender._MAXW = max(720, min(3840, int(m.get("maxw") or 1600)))
                                print(f"[BEM Support] stream quality → {rtc_sender._MAXW}px wide")
                            except Exception:
                                pass
                        elif t == "update":
                            if _EMBED:
                                # Embedded in a host app (Aria): the host owns its own
                                # exe lifecycle + hot-pulls the engine. Never run the
                                # standalone exe-updater (it targets BEMSupport.exe).
                                print("[BEM Support] update ignored — host app manages updates")
                            elif _RUN.get("in_session"):
                                print("[BEM Support] update deferred — a session is active")
                            else:
                                print("[BEM Support] operator requested update…")
                                if await asyncio.to_thread(_self_update, server):
                                    os._exit(0)
                finally:
                    bt.cancel()
        except Exception as e:
            msg = str(e)
            if "403" in msg:
                # Rejected: row removed (re-register) OR revoked (server refuses to
                # re-register → STOP, don't loop). register() returns a code on
                # success, None when the SERVER refused, raises on a network error.
                print("[BEM Support] link rejected — attempting re-register…")
                _set_status("●  Reconnecting…")
                try:
                    newcc = register(server, None, None, None, _load_config().get("group"))
                    if newcc:
                        c2 = _load()
                        if c2:
                            _RUN["aid"], _RUN["tok"] = c2["agent_id"], c2["token"]
                        _update_code(newcc)
                        print(f"[BEM Support] re-registered — your new code is {newcc}")
                        await asyncio.sleep(3)
                    else:
                        print("[BEM Support] access was removed by your technician — stopping.")
                        _set_status("●  Access removed — you can close this window.")
                        _remove_autostart()
                        return   # revoked/blocked: exit the loop instead of hammering the server
                except Exception:
                    print("[BEM Support] re-register network error — retry in 10s")
                    await asyncio.sleep(10)
            else:
                print("[BEM Support] control link dropped — reconnecting in 5s:", msg[:120])
                await asyncio.sleep(5)


def _autostart(exe=None):
    """Add a per-user autostart entry so the agent stays online (unattended).
    `exe` overrides the path (used by the onedir self-install to point at the
    installed copy, not the temporary unzipped one)."""
    try:
        import winreg
        if exe:
            cmd = f'"{exe}"'
        elif getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}"'
        else:
            cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "BEMSupport", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(k)
        print("[BEM Support] set to start automatically.")
    except Exception as e:
        print("[BEM Support] autostart skipped:", str(e)[:100])


# ── Admin mode (run elevated forever, no per-session UAC) ──────────────────────
# The ONLY Windows-sanctioned way to silent-elevate: a one-time elevated setup
# registers an at-logon HIGHEST-privilege scheduled task. After that, every logon
# launches the agent already-elevated (the user logs in as admin) → all fixes are
# headless with no prompt, ever. Creating the task needs ONE elevation (UAC Yes).
_TASK_NAME = "BEMRemoteSupport"


def _is_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _agent_exe():
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])


def _elevated_task_exists():
    try:
        import subprocess
        r = subprocess.run(["schtasks", "/query", "/tn", _TASK_NAME], capture_output=True,
                           text=True, timeout=15, stdin=subprocess.DEVNULL, creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


def _install_elevated_task():
    """Register an at-logon HIGHEST-privilege task → agent runs elevated every logon.
    Requires THIS process to be elevated. Drops the non-elevated HKCU Run key on success."""
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/create", "/tn", _TASK_NAME, "/tr", f'"{_agent_exe()}"',
                            "/sc", "onlogon", "/rl", "highest", "/f"], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL, creationflags=0x08000000)
        if r.returncode == 0:
            _remove_autostart()
            print("[BEM Support] admin mode installed — will run elevated at every logon.")
            return True
        print("[BEM Support] admin-task create failed:", (r.stderr or r.stdout or "").strip()[:150])
        return False
    except Exception as e:
        print("[BEM Support] admin-task error:", str(e)[:120]); return False


def _elevate_self():
    """One UAC consent on the client (no password for admin users) → relaunch elevated
    with --setup-elevated, which installs the logon task → permanent admin."""
    try:
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "runas", _agent_exe(), "--setup-elevated", None, 1)
        return True
    except Exception:
        return False


def _kill_other_agents():
    try:
        import subprocess
        subprocess.run(["taskkill", "/F", "/IM", "BEMSupport.exe", "/FI", f"PID ne {os.getpid()}"],
                       capture_output=True, stdin=subprocess.DEVNULL, creationflags=0x08000000, timeout=15)
    except Exception:
        pass


_SINGLETON_HANDLE = None  # keep the mutex handle alive for the whole process lifetime


def _single_instance():
    """Return True if we are the ONLY agent. A slow first launch made clients
    double/triple-click, spawning many agents that fought over one control link
    (flaky 'online', failed self-update, duplicate roster rows). One named mutex
    per machine = one agent; later clicks just no-op."""
    global _SINGLETON_HANDLE
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = wintypes.HANDLE
        _SINGLETON_HANDLE = k.CreateMutexW(None, False, "Global\\BEMRemoteSupport_singleton")
        return k.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True                       # never block startup on a guard failure


if __name__ == "__main__":
    # In a --windowed exe there is no console: stdout/stderr are None → guard so
    # print()/flush() never crash (the GUI window is the client's UI instead).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="wss://help.bem.solutions")
    ap.add_argument("--register")
    ap.add_argument("--label")
    ap.add_argument("--personal-key", dest="personal_key")
    ap.add_argument("--setup-elevated", action="store_true", dest="setup_elevated")
    a = ap.parse_args()
    # --onedir first run: copy the app to %LOCALAPPDATA%\BEMSupport + relaunch from
    # there (no temp extraction ever again). Done BEFORE the single-instance guard so
    # the installed copy acquires the mutex cleanly.
    if not getattr(a, "setup_elevated", False) and _onedir_install_and_relaunch():
        sys.exit(0)
    # Single-instance guard: a slow first launch made clients click repeatedly,
    # spawning many agents that fought over one control link. Extra copies no-op.
    # (--setup-elevated is exempt — it deliberately replaces the running copy.)
    if not getattr(a, "setup_elevated", False) and not _single_instance():
        try:
            if sys.stdout:
                print("[BEM Support] already running — not starting a second copy.")
        except Exception:
            pass
        sys.exit(0)
    cfg = _load_config()
    creds0 = _load()
    # Server precedence: explicit --server > saved creds > deploy config > default.
    server = a.server
    if a.server == "wss://help.bem.solutions":
        server = (creds0 or {}).get("server") or cfg.get("server") or a.server
    if getattr(a, "setup_elevated", False):
        # Relaunched via the one-time UAC elevation: install admin-mode autostart,
        # take over from the old non-elevated instance, and run elevated from here.
        if _is_elevated():
            _install_elevated_task()
            _kill_other_agents()
        if _load() is not None:
            try:
                asyncio.run(run(server))
            except KeyboardInterrupt:
                pass
        sys.exit(0)
    if _self_update(server):     # a newer exe is published → swap + relaunch, then exit
        sys.exit(0)
    if a.register:
        register(server, a.register, a.label, a.personal_key, cfg.get("group"))
    elif creds0 is None:
        # First run (double-clicked or mass-deployed): SELF-REGISTER — the client
        # enters NOTHING. A mass-deploy config can pre-set the group + server.
        print("Setting up BEM Remote Support…")
        register(server, None, a.label, a.personal_key, cfg.get("group"))
        # Managed/mass deploy (has a deploy config) → persist automatically. A plain
        # attended download stays OPT-IN: the client turns on Auto-support (button /
        # minimize / Settings) when they want it to keep running across reboots.
        if _load() is not None and (cfg.get("group") or cfg.get("autostart") or cfg.get("server")):
            _autostart()
    try:
        asyncio.run(run(server))
    except KeyboardInterrupt:
        print("\n[BEM Support] stopped.")
