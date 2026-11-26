---
name: teams-live-copilot
description: Listen to Windows Microsoft Teams system audio in real time via WASAPI Loopback and stream concise answers from Gemini Live API (gemini-3.8-live) in a floating Web UI or terminal, optionally grounded in a presentation/meeting context file. Use when the user wants to set up, install dependencies for, or launch real-time Teams meeting/presentation Q&A assistance.
---

# Teams Live Copilot Skill

Real-time Windows system audio listener (`PyAudioWPatch` WASAPI Loopback) + **Gemini Live API (`gemini-3.8-live`)** streaming text copilot. Designed for live Microsoft Teams meetings and presentation Q&A.

## Repository Contents

- `teams_live_copilot.py` — Main audio capture, resampling (16 kHz mono PCM), Gemini Live WebSocket client (`v1beta` with `v1alpha` / Vertex fallback), and built-in SSE Web UI + terminal UI.
- `gbsh0010_context.md` — Example presentation Q&A knowledge base (Group 36 · South Korea Doctors' Strike).
- `install.ps1` — One-click Windows environment installer (creates `.venv`, installs `requirements.txt`, optionally registers this folder as a global Antigravity skill).
- `start.ps1` / `start.bat` — One-click launcher that prompts for `GEMINI_API_KEY` (saved locally to `.env`) if missing and launches the Web UI.
- `requirements.txt` — Python dependencies (`google-genai`, `PyAudioWPatch`, `numpy`).

## Agent Instructions

When the user asks to **install** or **set up** this skill/tool on Windows:
1. Run `powershell -ExecutionPolicy Bypass -File install.ps1` in the skill/repo directory to create `.venv` and install all dependencies.
2. Verify audio devices with `.venv\Scripts\python.exe teams_live_copilot.py --list-devices`.
3. Remind the user to set `GEMINI_API_KEY` (or create `.env` with `GEMINI_API_KEY=...`).

When the user asks to **start** or **run** Teams live Q&A:
1. If they have a presentation/meeting document, pass it via `--context <path>` (or use `gbsh0010_context.md` if relevant).
2. Launch `.venv\Scripts\python.exe teams_live_copilot.py --context <path>` (or `start.bat`).
3. If Teams outputs audio to a specific headset/speaker other than the Windows default, pass `--device <index>` from `--list-devices`.
