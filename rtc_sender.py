#!/usr/bin/env python3
"""rtc_sender.py — Lux Remote Support PC-agent (WebRTC sender) for bem-pc.

Captures this machine's screen (dxcam) and streams it over WebRTC to the
/support browser viewer, and injects the viewer's mouse/keyboard (best-effort,
Win32). v1 targets bem-pc (BEM-owned) → auto-consents. The server half
(remote_support.py) is the signaling relay + session/ICE/audit.

Run:
  python rtc_sender.py --session <SID> --token <TOKEN>
        [--server wss://lux.bem.solutions] [--mode control|view] [--fps 12]

The AGENT is the WebRTC OFFERER (the viewer answers). Signaling messages:
  out: {"type":"offer","sdp":...}            in: {"type":"answer","sdp":...}
  both: {"type":"ice","candidate":{...}}
  in (data channel "input"): {"type":"input","kind":"mouse"|"keyboard",...}

desktop-app lane, 2026-06-22. (angelo-ops owns lux-observer; this is an additive
new file, operator-directed for the first end-to-end demo.)
"""
import argparse
import asyncio
import ctypes
import fractions
import json
import logging

import requests
import websockets
import dxcam
import av
from aiortc import (RTCPeerConnection, RTCConfiguration, RTCIceServer,
                    RTCSessionDescription, VideoStreamTrack)
from aiortc.sdp import candidate_from_sdp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rtc_sender")

user32 = ctypes.windll.user32
SCREEN_W = user32.GetSystemMetrics(0)
SCREEN_H = user32.GetSystemMetrics(1)


# ── screen capture track ─────────────────────────────────────────────────────
_CAM = None
_MAXW = 1600   # downscale wide screens before encoding (huge bandwidth/latency win;
               # control stays accurate — the viewer maps clicks by fraction, not pixels)


def _get_cam(fps: int):
    # One shared dxcam capture for the whole process — creating a second while
    # one is running raises, and we make a fresh ScreenTrack per viewer.
    global _CAM
    if _CAM is None:
        _CAM = dxcam.create(output_color="BGRA")  # native DXGI → no cv2 needed
        _CAM.start(target_fps=fps, video_mode=True)
    return _CAM


class ScreenTrack(VideoStreamTrack):
    def __init__(self, fps: int = 12):
        super().__init__()
        self.fps = fps
        self.cam = _get_cam(fps)

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = self.cam.get_latest_frame()
        while frame is None:
            await asyncio.sleep(1.0 / self.fps)
            frame = self.cam.get_latest_frame()
        # Convert + downscale OFF the event loop so the agent's heartbeat/control
        # link stay responsive during a session (else it ages out to "offline").
        vf = await asyncio.to_thread(self._encode_frame, frame)
        vf.pts = pts
        vf.time_base = time_base
        return vf

    @staticmethod
    def _encode_frame(frame):
        vf = av.VideoFrame.from_ndarray(frame, format="bgra")
        h, w = frame.shape[0], frame.shape[1]
        nw = min(w, _MAXW)
        nh = int(h * nw / w)
        nw -= nw % 2; nh -= nh % 2                       # yuv420p needs even dims
        return vf.reformat(width=nw, height=nh, format="yuv420p")  # downscale + encoder-native


# ── best-effort Win32 input injection ────────────────────────────────────────
_BTN = {0: (0x0002, 0x0004), 1: (0x0020, 0x0040), 2: (0x0008, 0x0010)}  # L/M/R down,up
_SPECIAL = {"Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B,
            " ": 0x20, "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27,
            "ArrowDown": 0x28, "Delete": 0x2E, "Home": 0x24, "End": 0x23}


def _inject(msg: dict):
    try:
        if msg.get("kind") == "mouse":
            sub = msg.get("sub")
            if sub == "move":
                # Absolute NORMALIZED move (0..65535 over the primary monitor) —
                # DPI- and resolution-independent, so display scaling (125/150%) on
                # the client no longer offsets the cursor. (viewer sends 0..10000.)
                fx = max(0, min(10000, msg.get("x") or 0)) / 10000
                fy = max(0, min(10000, msg.get("y") or 0)) / 10000
                ax, ay = int(fx * 65535), int(fy * 65535)
                user32.mouse_event(0x0001 | 0x8000, ax, ay, 0, 0)  # MOVE | ABSOLUTE
            elif sub in ("down", "up"):
                if msg.get("x") is not None:   # position exactly at the pressed point
                    fx = max(0, min(10000, msg.get("x") or 0)) / 10000
                    fy = max(0, min(10000, msg.get("y") or 0)) / 10000
                    user32.mouse_event(0x0001 | 0x8000, int(fx * 65535), int(fy * 65535), 0, 0)
                down, up = _BTN.get(int(msg.get("button") or 0), _BTN[0])
                user32.mouse_event(down if sub == "down" else up, 0, 0, 0, 0)
        elif msg.get("kind") == "keyboard":
            key = msg.get("key") or ""
            if key in _SPECIAL:
                vk = _SPECIAL[key]
            elif len(key) == 1:
                vk = user32.VkKeyScanW(ord(key)) & 0xFF
            else:
                return
            user32.keybd_event(vk, 0, 0, 0)      # down
            user32.keybd_event(vk, 0, 2, 0)      # up (KEYEVENTF_KEYUP)
    except Exception as e:
        log.warning("inject failed: %s", e)


# ── main ─────────────────────────────────────────────────────────────────────
async def run(args):
    http = args.server.replace("wss://", "https://").replace("ws://", "http://")
    sid, tok = args.session, args.token

    # auto-consent (bem-pc is BEM-owned). In the public product a human clicks.
    print("=" * 58)
    print(f"  BEM Remote Support — session {sid} is going LIVE ({args.mode}).")
    print("  This PC's screen is being shared. Ctrl+C here to STOP.")
    print("=" * 58)
    r = requests.post(f"{http}/api/support/{sid}/consent",
                      json={"mode": args.mode, "token": tok}, timeout=10)
    log.info("consent -> %s %s", r.status_code, r.text[:120])

    ice = requests.get(f"{http}/api/support/ice", timeout=10).json()
    servers = []
    for s in ice.get("iceServers", []):
        servers.append(RTCIceServer(urls=s["urls"],
                                    username=s.get("username"),
                                    credential=s.get("credential")))
    config = RTCConfiguration(iceServers=servers)
    state = {"pc": None}

    def make_pc():
        pc = RTCPeerConnection(config)
        ch = pc.createDataChannel("input")
        @ch.on("message")
        def on_msg(m):
            try:
                d = json.loads(m)
                if d.get("type") == "input" and args.mode == "control":
                    _inject(d)
            except Exception:
                pass
        pc.addTrack(ScreenTrack(args.fps))
        @pc.on("connectionstatechange")
        async def _state():
            log.info("pc state -> %s", pc.connectionState)
        return pc

    url = f"{args.server}/api/support/signal/{sid}?role=agent&token={tok}"
    _ctx = None                                   # frozen-macOS wss cert fix (see agent._wss_ssl)
    if str(url).startswith("wss"):
        try:
            import ssl as _ssl, certifi
            _ctx = _ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = None
    async with websockets.connect(url, max_size=2**22, ssl=_ctx) as ws:
        log.info("signaling connected; waiting for viewer (peer_ready)")

        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            if t == "peer_ready":
                # fresh peer connection per viewer (handles reconnects)
                if state["pc"] is not None:
                    try:
                        await state["pc"].close()
                    except Exception:
                        pass
                state["pc"] = make_pc()
                log.info("viewer present — creating + sending offer")
                offer = await state["pc"].createOffer()
                await state["pc"].setLocalDescription(offer)
                await ws.send(json.dumps({"type": "offer", "sdp": state["pc"].localDescription.sdp}))
            elif t == "answer" and state["pc"] is not None:
                await state["pc"].setRemoteDescription(RTCSessionDescription(sdp=m["sdp"], type="answer"))
                log.info("answer applied — streaming")
            elif t == "ice" and m.get("candidate") and state["pc"] is not None:
                try:
                    c = m["candidate"]
                    cand = candidate_from_sdp(c["candidate"].split(":", 1)[1])
                    cand.sdpMid = c.get("sdpMid")
                    cand.sdpMLineIndex = c.get("sdpMLineIndex")
                    await state["pc"].addIceCandidate(cand)
                except Exception as e:
                    log.warning("ice add failed: %s", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--server", default="wss://lux.bem.solutions")
    ap.add_argument("--mode", default="control", choices=["control", "view"])
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args()
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("\nRemote Support session ended.")
