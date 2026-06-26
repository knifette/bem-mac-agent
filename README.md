# BEM Remote Support — macOS agent

Native macOS port of the Windows BEM Remote Support agent, **built + signed on
Codemagic's cloud Macs** (no Mac owned — same pipeline as the BEM iOS apps).
Ships as a Developer-ID-notarized `.dmg`.

## Status
- **Phase 1 (in progress):** stand up the Codemagic build pipeline, then port the
  Windows-specific bits of `agent.py` to macOS (launchd autostart, `~/Library`
  install, lock-file single-instance, zsh `run_cmd`). The engine (aiortc WebRTC
  screen share, the WS control loop, screen capture, file transfer) is already
  cross-platform.
- **Phase 2:** live mouse/keyboard control (Accessibility + Quartz) + admin
  elevation (osascript).

## Build (Codemagic)
`codemagic.yaml` → `mac-agent` workflow → PyInstaller `.app` → DMG. First builds
are **unsigned** (prove the pipeline); Developer ID signing + notarization is the
TODO at the bottom of `codemagic.yaml`.

## Source
- `agent.py` — the agent (shared with the Windows build at
  `\\bemserver\OPS\lux\bridges\lux-observer\agent.py`; macOS branches added here).
- `rtc_sender.py` — the WebRTC screen sender (cross-platform, mss + aiortc).

Server: `wss://help.bem.solutions`. See the lux repo `bridges/lux-observer/SUPPORT_CORE.md`
and memory `project_native_mac_agent` for the full plan + human gates.
