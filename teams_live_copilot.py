"""Real-time Windows Teams audio listener & Gemini Live API text copilot.

Captures Windows system output audio via WASAPI Loopback (PyAudioWPatch),
resamples to 16 kHz mono 16-bit PCM, streams to Gemini Live API
(gemini-3.8-live), and displays streaming text answers in either a lightweight
always-on-top Tkinter overlay window or the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import scrolledtext
from typing import Optional

import numpy as np
from google import genai
from google.genai import types

if sys.platform == "win32":
    import pyaudiowpatch as pyaudio

TARGET_SAMPLE_RATE = 16000
CHUNK_DURATION_MS = 100  # 100 ms chunks for low latency
DEFAULT_MODEL = "gemini-3.8-live"

DEFAULT_SYSTEM_INSTRUCTION = """You are a silent, real-time meeting & interview copilot listening to a Microsoft Teams call.
Your job:
1. Listen continuously to the incoming audio stream.
2. When someone asks a question, raises a technical/business problem, or requests an opinion/explanation, immediately provide a clear, concise, well-structured answer in the same language as the question (or Chinese/English as appropriate).
3. Put the direct answer first (1-3 bullet points or a short direct paragraph), followed by brief supporting details if helpful.
4. If the speakers are only making small talk, greetings, or incomplete filler ("uh", "okay", "can you hear me"), stay brief or wait until a real question or topic is clear.
5. Do not repeat the question verbatim; go straight to the answer."""


@dataclass
class UIEvent:
    kind: str  # "status" | "transcript" | "answer_chunk" | "answer_end" | "error"
    text: str = ""


def list_loopback_devices() -> list[dict]:
    """Return available Windows WASAPI loopback devices."""
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    devices: list[dict] = []
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        default_name = default_speakers.get("name", "")
        for loopback in p.get_loopback_device_info_generator():
            devices.append(
                {
                    "index": loopback["index"],
                    "name": loopback["name"],
                    "channels": int(loopback["maxInputChannels"]),
                    "rate": int(loopback["defaultSampleRate"]),
                    "is_default": default_name in loopback["name"],
                }
            )
    finally:
        p.terminate()
    return devices


def resolve_loopback_device(p, device_index: Optional[int]) -> dict:
    """Resolve the requested WASAPI loopback device or the default speaker loopback."""
    if device_index is not None:
        info = p.get_device_info_by_index(device_index)
        if not info.get("isLoopbackDevice", False):
            raise ValueError(
                f"Device [{device_index}] '{info.get('name')}' is not a WASAPI loopback device. "
                "Run with --list-devices to see valid loopback indices."
            )
        return info

    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
    if not default_speakers.get("isLoopbackDevice", False):
        for loopback in p.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                return loopback
        raise RuntimeError(
            "Could not find a WASAPI loopback device matching the default output speaker."
        )
    return default_speakers


def resample_to_16k_mono_pcm(
    raw_bytes: bytes, src_rate: int, src_channels: int
) -> bytes:
    """Convert raw int16 interleaved audio into 16 kHz mono int16 PCM bytes."""
    samples = np.frombuffer(raw_bytes, dtype=np.int16)
    if samples.size == 0:
        return b""

    if src_channels > 1:
        usable = (samples.size // src_channels) * src_channels
        samples = samples[:usable].reshape(-1, src_channels).mean(axis=1)
    else:
        samples = samples.astype(np.float32)

    if src_rate != TARGET_SAMPLE_RATE and samples.size > 1:
        target_len = max(1, int(round(samples.size * TARGET_SAMPLE_RATE / src_rate)))
        x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        samples = np.interp(x_new, x_old, samples)

    pcm16 = np.clip(np.round(samples), -32768, 32767).astype(np.int16)
    return pcm16.tobytes()


class GeminiTeamsWorker:
    """Background worker capturing system/Teams audio and streaming to Gemini Live API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        api_version: str,
        vertexai: bool,
        device_index: Optional[int],
        app_filter: Optional[str],
        system_instruction: str,
        event_queue: queue.Queue[UIEvent],
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_version = api_version
        self.vertexai = vertexai
        self.device_index = device_index
        self.app_filter = app_filter
        self.system_instruction = system_instruction
        self.event_queue = event_queue
        self.stop_event = stop_event
        self.pause_event = pause_event
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def _emit(self, kind: str, text: str = "") -> None:
        self.event_queue.put(UIEvent(kind=kind, text=text))

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as exc:
            self._emit("error", f"Worker stopped: {exc}")

    def _ensure_mac_audio_tap(self) -> str:
        """Return path to compiled macOS ScreenCaptureKit helper, compiling if needed."""
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        bin_path = os.path.join(repo_dir, "mac", "teams_audio_tap")
        swift_path = os.path.join(repo_dir, "mac", "AudioTap.swift")
        if os.path.isfile(bin_path) and os.access(bin_path, os.X_OK):
            return bin_path
        if not os.path.isfile(swift_path):
            raise RuntimeError(f"macOS audio helper source not found: {swift_path}")
        self._emit("status", "Compiling macOS ScreenCaptureKit audio helper...")
        subprocess.run(
            ["swiftc", "-O", "-parse-as-library", swift_path, "-o", bin_path],
            check=True,
        )
        return bin_path

    async def _async_main(self) -> None:
        audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()

        cleanup_callbacks: list = []
        if sys.platform == "darwin":
            try:
                bin_path = self._ensure_mac_audio_tap()
            except Exception as exc:
                self._emit("error", f"macOS audio helper setup failed: {exc}")
                return

            cmd = [bin_path]
            if self.app_filter:
                cmd.extend(["--app", self.app_filter])
            device_name = (
                f"macOS ScreenCaptureKit ({self.app_filter})"
                if self.app_filter
                else "macOS ScreenCaptureKit (All System Audio)"
            )
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            def _mac_stdout_reader() -> None:
                chunk_bytes = int(TARGET_SAMPLE_RATE * CHUNK_DURATION_MS / 1000) * 2
                assert proc.stdout is not None
                while not self.stop_event.is_set() and proc.poll() is None:
                    data = proc.stdout.read(chunk_bytes)
                    if not data:
                        break
                    if not self.pause_event.is_set():
                        def _put(buf=data):
                            if audio_queue.full():
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            audio_queue.put_nowait(buf)

                        loop.call_soon_threadsafe(_put)

            def _mac_stderr_reader() -> None:
                assert proc.stderr is not None
                for raw_line in proc.stderr:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line:
                        self._emit("error" if "error" in line.lower() else "status", line)

            threading.Thread(target=_mac_stdout_reader, daemon=True).start()
            threading.Thread(target=_mac_stderr_reader, daemon=True).start()

            def _cleanup_mac() -> None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            cleanup_callbacks.append(_cleanup_mac)
            self._emit("status", f"Listening: {device_name}")
        else:
            import pyaudiowpatch as pyaudio

            p = pyaudio.PyAudio()
            try:
                device_info = resolve_loopback_device(p, self.device_index)
            except Exception as exc:
                p.terminate()
                self._emit("error", str(exc))
                return

            src_rate = int(device_info["defaultSampleRate"])
            src_channels = int(device_info["maxInputChannels"])
            frames_per_buffer = max(256, int(src_rate * CHUNK_DURATION_MS / 1000))
            device_name = device_info["name"]

            def audio_callback(in_data, frame_count, time_info, status_flags):
                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    pcm = resample_to_16k_mono_pcm(in_data, src_rate, src_channels)
                    if pcm:
                        def _put():
                            if audio_queue.full():
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            audio_queue.put_nowait(pcm)

                        loop.call_soon_threadsafe(_put)
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=src_channels,
                rate=src_rate,
                input=True,
                input_device_index=int(device_info["index"]),
                frames_per_buffer=frames_per_buffer,
                stream_callback=audio_callback,
            )
            stream.start_stream()

            def _cleanup_win() -> None:
                stream.stop_stream()
                stream.close()
                p.terminate()

            cleanup_callbacks.append(_cleanup_win)
            self._emit("status", f"Listening: {device_name}")

        # Build candidate (vertexai, api_version) endpoints; google-genai defaults to v1,
        # whereas Live API WebSockets require v1beta / v1alpha (or v1beta1 on Vertex).
        candidates: list[tuple[bool, str]] = [(self.vertexai, self.api_version)]
        if not self.vertexai:
            if self.api_version != "v1alpha":
                candidates.append((False, "v1alpha"))
            if self.api_key.startswith("AQ."):
                candidates.append((True, "v1beta1"))
        candidate_idx = 0

        resumption_handle: Optional[str] = None

        try:
            while not self.stop_event.is_set():
                use_vertex, use_version = candidates[candidate_idx]
                client = genai.Client(
                    api_key=self.api_key,
                    vertexai=use_vertex,
                    http_options=types.HttpOptions(api_version=use_version),
                )
                resumption_cfg = (
                    types.SessionResumptionConfig(handle=resumption_handle)
                    if resumption_handle
                    else types.SessionResumptionConfig()
                )
                config = types.LiveConnectConfig(
                    response_modalities=[types.Modality.AUDIO],
                    system_instruction=types.Content(
                        parts=[types.Part(text=self.system_instruction)]
                    ),
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    output_audio_transcription=types.AudioTranscriptionConfig(),
                    context_window_compression=types.ContextWindowCompressionConfig(
                        sliding_window=types.SlidingWindow()
                    ),
                    session_resumption=resumption_cfg,
                )

                try:
                    endpoint_tag = "vertex" if use_vertex else use_version
                    self._emit("status", f"Connecting ({self.model}, {endpoint_tag}) | {device_name}")
                    async with client.aio.live.connect(
                        model=self.model, config=config
                    ) as session:
                        self._emit("status", f"Connected ({self.model}, {endpoint_tag}) | {device_name}")
                        sender = asyncio.create_task(
                            self._send_audio_loop(session, audio_queue)
                        )
                        receiver = asyncio.create_task(
                            self._receive_loop(session)
                        )
                        done, pending = await asyncio.wait(
                            {sender, receiver},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        for task in done:
                            exc = task.exception()
                            if exc is not None:
                                raise exc
                        if hasattr(self, "_latest_handle") and self._latest_handle:
                            resumption_handle = self._latest_handle
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    msg = str(exc)
                    if "404" in msg and candidate_idx + 1 < len(candidates):
                        candidate_idx += 1
                        next_v, next_ver = candidates[candidate_idx]
                        self._emit(
                            "error",
                            f"Endpoint {use_version} returned 404, trying {'vertex/' if next_v else ''}{next_ver}...",
                        )
                        await asyncio.sleep(0.5)
                        continue
                    self._emit("error", f"Connection reset ({exc}), reconnecting in 2s...")
                    await asyncio.sleep(2.0)
        finally:
            for cb in cleanup_callbacks:
                try:
                    cb()
                except Exception:
                    pass
            self._emit("status", "Stopped")

    async def _send_audio_loop(
        self, session, audio_queue: asyncio.Queue[bytes]
    ) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )

    async def _receive_loop(self, session) -> None:
        answering = False
        async for response in session.receive():
            if self.stop_event.is_set():
                break

            resumption = getattr(response, "session_resumption_update", None)
            if resumption and getattr(resumption, "new_handle", None):
                self._latest_handle = resumption.new_handle

            content = getattr(response, "server_content", None)
            if not content:
                continue

            if getattr(content, "input_transcription", None):
                text = content.input_transcription.text
                if text:
                    self._emit("transcript", text)

            if getattr(content, "output_transcription", None):
                text = content.output_transcription.text
                if text:
                    answering = True
                    self._emit("answer_chunk", text)

            if getattr(content, "turn_complete", False) or getattr(
                content, "interrupted", False
            ):
                if answering:
                    self._emit("answer_end", "")
                    answering = False


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Wrap raw 16 kHz mono int16 PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class VadChatCopilotWorker(GeminiTeamsWorker):
    """Audio copilot for OpenAI GPT and Anthropic Claude using RMS VAD + STT + streaming LLM."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        device_index: Optional[int],
        app_filter: Optional[str],
        system_instruction: str,
        event_queue: queue.Queue[UIEvent],
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            api_version="v1beta",
            vertexai=False,
            device_index=device_index,
            app_filter=app_filter,
            system_instruction=system_instruction,
            event_queue=event_queue,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        self.provider = provider
        self._history: list[dict[str, str]] = []

    async def _async_main(self) -> None:
        audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        loop = asyncio.get_running_loop()
        cleanup_callbacks: list = []

        if sys.platform == "darwin":
            try:
                bin_path = self._ensure_mac_audio_tap()
            except Exception as exc:
                self._emit("error", f"macOS audio helper setup failed: {exc}")
                return
            cmd = [bin_path]
            if self.app_filter:
                cmd.extend(["--app", self.app_filter])
            device_name = (
                f"macOS ScreenCaptureKit ({self.app_filter})"
                if self.app_filter
                else "macOS ScreenCaptureKit (All System Audio)"
            )
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

            def _mac_stdout_reader() -> None:
                chunk_bytes = int(TARGET_SAMPLE_RATE * CHUNK_DURATION_MS / 1000) * 2
                assert proc.stdout is not None
                while not self.stop_event.is_set() and proc.poll() is None:
                    data = proc.stdout.read(chunk_bytes)
                    if not data:
                        break
                    if not self.pause_event.is_set():
                        def _put(buf=data):
                            if audio_queue.full():
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            audio_queue.put_nowait(buf)
                        loop.call_soon_threadsafe(_put)

            threading.Thread(target=_mac_stdout_reader, daemon=True).start()
            cleanup_callbacks.append(lambda: proc.terminate() if proc.poll() is None else None)
        else:
            import pyaudiowpatch as pyaudio

            p = pyaudio.PyAudio()
            try:
                device_info = resolve_loopback_device(p, self.device_index)
            except Exception as exc:
                p.terminate()
                self._emit("error", str(exc))
                return
            src_rate = int(device_info["defaultSampleRate"])
            src_channels = int(device_info["maxInputChannels"])
            frames_per_buffer = max(256, int(src_rate * CHUNK_DURATION_MS / 1000))
            device_name = device_info["name"]

            def audio_callback(in_data, frame_count, time_info, status_flags):
                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    pcm = resample_to_16k_mono_pcm(in_data, src_rate, src_channels)
                    if pcm:
                        def _put():
                            if audio_queue.full():
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            audio_queue.put_nowait(pcm)
                        loop.call_soon_threadsafe(_put)
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=src_channels,
                rate=src_rate,
                input=True,
                input_device_index=int(device_info["index"]),
                frames_per_buffer=frames_per_buffer,
                stream_callback=audio_callback,
            )
            stream.start_stream()
            cleanup_callbacks.append(lambda: (stream.stop_stream(), stream.close(), p.terminate()))

        self._emit("status", f"Connected ({self.provider}: {self.model}) | {device_name}")

        # Simple RMS energy VAD: accumulate speech chunks until ~0.9s of silence
        speech_buf = bytearray()
        in_speech = False
        silence_chunks = 0
        rms_threshold = 120.0
        silence_limit = 11  # 11 * 100ms = 1.1s

        try:
            import httpx

            async with httpx.AsyncClient(timeout=60.0) as http:
                while not self.stop_event.is_set():
                    try:
                        chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue

                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

                    if rms >= rms_threshold:
                        in_speech = True
                        silence_chunks = 0
                        speech_buf.extend(chunk)
                    elif in_speech:
                        speech_buf.extend(chunk)
                        silence_chunks += 1
                        if silence_chunks >= silence_limit or len(speech_buf) >= TARGET_SAMPLE_RATE * 2 * 20:
                            pcm_utterance = bytes(speech_buf)
                            speech_buf.clear()
                            in_speech = False
                            silence_chunks = 0
                            if len(pcm_utterance) >= TARGET_SAMPLE_RATE * 2 * 1.0:
                                await self._handle_utterance(http, pcm_utterance)
        finally:
            for cb in cleanup_callbacks:
                try:
                    cb()
                except Exception:
                    pass
            self._emit("status", "Stopped")

    async def _transcribe(self, http: httpx.AsyncClient, pcm_bytes: bytes) -> str:
        wav_bytes = pcm16_to_wav_bytes(pcm_bytes)
        openai_key = os.environ.get("OPENAI_API_KEY")
        gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

        if self.provider == "gpt" or (self.provider == "claude" and openai_key and not gemini_key):
            key = self.api_key if self.provider == "gpt" else openai_key
            resp = await http.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"model": "whisper-1"},
            )
            if resp.status_code == 429 and gemini_key:
                pass
            elif resp.is_error:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            else:
                return resp.json().get("text", "").strip()

        if gemini_key:
            client = genai.Client(api_key=gemini_key)
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-flash",
                contents=[
                    "Transcribe this speech verbatim. Output only the transcript text.",
                    types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                ],
            )
            return (resp.text or "").strip()

        raise RuntimeError(
            "Claude does not natively accept audio. Also set GEMINI_API_KEY or OPENAI_API_KEY in .env for speech-to-text."
        )

    async def _handle_utterance(self, http: httpx.AsyncClient, pcm_bytes: bytes) -> None:
        try:
            transcript = await self._transcribe(http, pcm_bytes)
        except Exception as exc:
            self._emit("error", f"Transcription error: {exc}")
            return

        if not transcript or len(transcript) < 3:
            return
        self._emit("transcript", transcript + "\n")

        self._history.append({"role": "user", "content": transcript})
        if len(self._history) > 12:
            self._history = self._history[-12:]

        answer_parts: list[str] = []
        try:
            if self.provider == "gpt":
                async with http.stream(
                    "POST",
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "stream": True,
                        "messages": [
                            {"role": "system", "content": self.system_instruction},
                            *self._history,
                        ],
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        data = json.loads(payload)
                        delta = data["choices"][0]["delta"].get("content")
                        if delta:
                            answer_parts.append(delta)
                            self._emit("answer_chunk", delta)
            else:
                async with http.stream(
                    "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 512,
                        "stream": True,
                        "system": self.system_instruction,
                        "messages": self._history,
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = json.loads(line[6:].strip())
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {}).get("text")
                            if delta:
                                answer_parts.append(delta)
                                self._emit("answer_chunk", delta)
        except Exception as exc:
            self._emit("error", f"{self.provider.upper()} API error: {exc}")
            return

        if answer_parts:
            self._history.append({"role": "assistant", "content": "".join(answer_parts)})
            self._emit("answer_end", "")

    async def _send_audio_loop(
        self, session, audio_queue: asyncio.Queue[bytes]
    ) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )

    async def _receive_loop(self, session) -> None:
        answering = False
        async for response in session.receive():
            if self.stop_event.is_set():
                break

            resumption = getattr(response, "session_resumption_update", None)
            if resumption and getattr(resumption, "new_handle", None):
                self._latest_handle = resumption.new_handle

            content = getattr(response, "server_content", None)
            if not content:
                continue

            if getattr(content, "input_transcription", None):
                text = content.input_transcription.text
                if text:
                    self._emit("transcript", text)

            if getattr(content, "output_transcription", None):
                text = content.output_transcription.text
                if text:
                    answering = True
                    self._emit("answer_chunk", text)

            if getattr(content, "turn_complete", False) or getattr(
                content, "interrupted", False
            ):
                if answering:
                    self._emit("answer_end", "")
                    answering = False


class OverlayWindow:
    """Lightweight always-on-top dark overlay for live Teams answers."""

    def __init__(
        self,
        event_queue: queue.Queue[UIEvent],
        stop_event: threading.Event,
        pause_event: threading.Event,
    ) -> None:
        self.event_queue = event_queue
        self.stop_event = stop_event
        self.pause_event = pause_event
        self._answering = False

        self.root = tk.Tk()
        self.root.title("Teams Live Copilot (Gemini)")
        self.root.geometry("560x640+40+60")
        self.root.configure(bg="#111827")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        top_bar = tk.Frame(self.root, bg="#1F2937", padx=10, pady=6)
        top_bar.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="Initializing...")
        status_lbl = tk.Label(
            top_bar,
            textvariable=self.status_var,
            bg="#1F2937",
            fg="#93C5FD",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.pause_btn = tk.Button(
            top_bar,
            text="Pause",
            command=self._toggle_pause,
            bg="#374151",
            fg="#F9FAFB",
            relief=tk.FLAT,
            padx=8,
            font=("Segoe UI", 9),
        )
        self.pause_btn.pack(side=tk.RIGHT, padx=(6, 0))

        clear_btn = tk.Button(
            top_bar,
            text="Clear",
            command=self._clear_text,
            bg="#374151",
            fg="#F9FAFB",
            relief=tk.FLAT,
            padx=8,
            font=("Segoe UI", 9),
        )
        clear_btn.pack(side=tk.RIGHT)

        self.text_area = scrolledtext.ScrolledText(
            self.root,
            wrap=tk.WORD,
            bg="#111827",
            fg="#F3F4F6",
            insertbackground="#F3F4F6",
            font=("Microsoft YaHei UI", 10),
            relief=tk.FLAT,
            padx=12,
            pady=10,
        )
        self.text_area.pack(fill=tk.BOTH, expand=True)
        self.text_area.tag_config("transcript", foreground="#9CA3AF", font=("Microsoft YaHei UI", 9))
        self.text_area.tag_config("answer_hdr", foreground="#34D399", font=("Microsoft YaHei UI", 10, "bold"))
        self.text_area.tag_config("answer", foreground="#F9FAFB", font=("Microsoft YaHei UI", 10))
        self.text_area.tag_config("error", foreground="#FCA5A5", font=("Microsoft YaHei UI", 9))
        self.text_area.configure(state=tk.DISABLED)

    def _toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="Pause", bg="#374151")
            self.status_var.set("Resumed listening")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="Resume", bg="#B45309")
            self.status_var.set("Paused")

    def _clear_text(self) -> None:
        self.text_area.configure(state=tk.NORMAL)
        self.text_area.delete("1.0", tk.END)
        self.text_area.configure(state=tk.DISABLED)

    def _append(self, text: str, tag: str) -> None:
        self.text_area.configure(state=tk.NORMAL)
        self.text_area.insert(tk.END, text, tag)
        self.text_area.see(tk.END)
        self.text_area.configure(state=tk.DISABLED)

    def _poll_events(self) -> None:
        while True:
            try:
                ev = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if ev.kind == "status":
                self.status_var.set(ev.text)
            elif ev.kind == "transcript":
                if self._answering:
                    self._append("\n\n", "answer")
                    self._answering = False
                self._append(ev.text, "transcript")
            elif ev.kind == "answer_chunk":
                if not self._answering:
                    self._append("\n[AI Answer]\n", "answer_hdr")
                    self._answering = True
                self._append(ev.text, "answer")
            elif ev.kind == "answer_end":
                if self._answering:
                    self._append("\n" + "-" * 40 + "\n", "transcript")
                    self._answering = False
            elif ev.kind == "error":
                self._append(f"\n[Warning] {ev.text}\n", "error")

        if not self.stop_event.is_set():
            self.root.after(50, self._poll_events)

    def _on_close(self) -> None:
        self.stop_event.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.after(50, self._poll_events)
        self.root.mainloop()


WEB_UI_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Teams Live Copilot (Gemini)</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; background: #0f172a; color: #f1f5f9; display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; background: #1e293b; border-bottom: 1px solid #334155; gap: 10px; }
  #status { font-size: 13px; font-weight: 600; color: #93c5fd; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btns { display: flex; gap: 8px; flex-shrink: 0; }
  button { background: #334155; color: #f8fafc; border: none; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  button:hover { background: #475569; }
  button.paused { background: #b45309; }
  main { flex: 1; overflow-y: auto; padding: 14px; line-height: 1.6; font-size: 14px; white-space: pre-wrap; word-break: break-word; }
  .transcript { color: #94a3b8; font-size: 13px; }
  .answer-hdr { color: #34d399; font-weight: 700; margin-top: 10px; display: block; }
  .answer { color: #f8fafc; }
  .error { color: #fca5a5; font-size: 13px; display: block; margin-top: 6px; }
  .sep { border-top: 1px solid #334155; margin: 10px 0; }
</style>
</head>
<body>
<header>
  <div id="status">Initializing...</div>
  <div class="btns">
    <button id="pauseBtn" onclick="togglePause()">Pause</button>
    <button onclick="clearLog()">Clear</button>
  </div>
</header>
<main id="log"></main>
<script>
  const logEl = document.getElementById('log');
  const statusEl = document.getElementById('status');
  const pauseBtn = document.getElementById('pauseBtn');
  let curSpan = null;
  let answering = false;

  function appendSpan(cls, text) {
    if (!curSpan || curSpan.className !== cls) {
      curSpan = document.createElement('span');
      curSpan.className = cls;
      logEl.appendChild(curSpan);
    }
    curSpan.textContent += text;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function clearLog() {
    logEl.innerHTML = '';
    curSpan = null;
    answering = false;
  }

  async function togglePause() {
    const res = await fetch('/toggle-pause', { method: 'POST' });
    const data = await res.json();
    pauseBtn.textContent = data.paused ? 'Resume' : 'Pause';
    pauseBtn.className = data.paused ? 'paused' : '';
    statusEl.textContent = data.paused ? 'Paused' : 'Resumed listening';
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.kind === 'status') {
      statusEl.textContent = ev.text;
    } else if (ev.kind === 'transcript') {
      if (answering) {
        logEl.appendChild(document.createElement('div')).className = 'sep';
        curSpan = null;
        answering = false;
      }
      appendSpan('transcript', ev.text);
    } else if (ev.kind === 'answer_chunk') {
      if (!answering) {
        const hdr = document.createElement('span');
        hdr.className = 'answer-hdr';
        hdr.textContent = '[AI Answer]\\n';
        logEl.appendChild(hdr);
        curSpan = null;
        answering = true;
      }
      appendSpan('answer', ev.text);
    } else if (ev.kind === 'answer_end') {
      if (answering) {
        logEl.appendChild(document.createElement('div')).className = 'sep';
        curSpan = null;
        answering = false;
      }
    } else if (ev.kind === 'error') {
      const err = document.createElement('span');
      err.className = 'error';
      err.textContent = '[Warning] ' + ev.text + '\\n';
      logEl.appendChild(err);
      curSpan = null;
    }
  };
</script>
</body>
</html>
"""


def run_web_ui(
    event_queue: queue.Queue[UIEvent],
    stop_event: threading.Event,
    pause_event: threading.Event,
    port: int = 0,
) -> None:
    """Serve a lightweight dark-mode SSE web UI and open it in the default browser."""
    subscribers: list[queue.Queue[UIEvent]] = []
    sub_lock = threading.Lock()
    latest_status = UIEvent(kind="status", text="Starting...")

    def broadcaster() -> None:
        nonlocal latest_status
        while not stop_event.is_set():
            try:
                ev = event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev.kind == "status":
                latest_status = ev
            with sub_lock:
                for q_sub in list(subscribers):
                    q_sub.put(ev)

    threading.Thread(target=broadcaster, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/":
                body = WEB_UI_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                sub_q: queue.Queue[UIEvent] = queue.Queue()
                sub_q.put(latest_status)
                with sub_lock:
                    subscribers.append(sub_q)
                try:
                    while not stop_event.is_set():
                        try:
                            ev = sub_q.get(timeout=0.5)
                            payload = json.dumps(
                                {"kind": ev.kind, "text": ev.text}, ensure_ascii=False
                            )
                            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with sub_lock:
                        if sub_q in subscribers:
                            subscribers.remove(sub_q)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/toggle-pause":
                if pause_event.is_set():
                    pause_event.clear()
                else:
                    pause_event.set()
                body = json.dumps({"paused": pause_event.is_set()}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Web UI running at {url} (Press Ctrl+C to stop)", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


def run_terminal_ui(
    event_queue: queue.Queue[UIEvent], stop_event: threading.Event
) -> None:
    """Stream events directly to stdout."""
    answering = False
    try:
        while not stop_event.is_set():
            try:
                ev = event_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if ev.kind == "status":
                print(f"\n[Status] {ev.text}", flush=True)
            elif ev.kind == "transcript":
                if answering:
                    print("\n", flush=True)
                    answering = False
                print(ev.text, end="", flush=True)
            elif ev.kind == "answer_chunk":
                if not answering:
                    print("\n\n[AI Answer] ", end="", flush=True)
                    answering = True
                print(ev.text, end="", flush=True)
            elif ev.kind == "answer_end":
                if answering:
                    print("\n" + "-" * 40, flush=True)
                    answering = False
            elif ev.kind == "error":
                print(f"\n[Warning] {ev.text}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        stop_event.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen to Windows Teams system audio and stream Gemini Live API answers."
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available Windows WASAPI loopback devices and exit.",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Windows WASAPI loopback device index (defaults to system default speaker).",
    )
    parser.add_argument(
        "--app",
        default="Microsoft Teams",
        help="macOS ScreenCaptureKit app filter (default: 'Microsoft Teams').",
    )
    parser.add_argument(
        "--all-audio",
        action="store_true",
        help="On macOS, capture all system audio instead of filtering to --app.",
    )
    parser.add_argument(
        "--ui",
        choices=["auto", "overlay", "web", "terminal"],
        default="auto",
        help="Display mode: 'auto' (Tk overlay with Web UI fallback), 'web', 'overlay', or 'terminal'.",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "gpt", "claude"],
        default=None,
        help="AI provider: 'gemini' (default), 'gpt', or 'claude'.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override (defaults: gemini-3.8-live / gpt-4o-mini / claude-sonnet-4-5).",
    )
    parser.add_argument(
        "--api-version",
        default="v1beta",
        help="Gemini API version for Live WebSocket (default: v1beta).",
    )
    parser.add_argument(
        "--vertexai",
        action="store_true",
        help="Use Vertex AI endpoint instead of Gemini Developer API.",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_SYSTEM_INSTRUCTION,
        help="Custom system instruction for the copilot.",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Path to a Markdown/text context file appended to system instruction.",
    )
    return parser.parse_args()


def load_local_dotenv() -> None:
    """Load key=value pairs from .env next to the script if not already in os.environ."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def main() -> int:
    load_local_dotenv()
    args = parse_args()

    if args.list_devices:
        if sys.platform == "darwin":
            print("On macOS, ScreenCaptureKit captures Microsoft Teams directly (no device index needed).")
            print("Use --all-audio to capture all system audio, or --app <name> to filter an app.")
            return 0
        devices = list_loopback_devices()
        if not devices:
            print("No WASAPI loopback devices found.")
            return 1
        print("Available Windows WASAPI loopback devices:")
        for d in devices:
            mark = " (default)" if d["is_default"] else ""
            print(
                f"  [{d['index']}] {d['name']} | {d['rate']} Hz | {d['channels']} ch{mark}"
            )
        return 0

    provider = (args.provider or os.environ.get("AI_PROVIDER") or "gemini").lower()
    default_models = {
        "gemini": DEFAULT_MODEL,
        "gpt": "gpt-4o-mini",
        "claude": "claude-sonnet-4-5",
    }
    model = args.model or default_models[provider]

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        key_name = "GEMINI_API_KEY"
    elif provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY")
        key_name = "OPENAI_API_KEY"
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        key_name = "ANTHROPIC_API_KEY"

    if not api_key:
        print(
            f"Error: {key_name} environment variable is not set for provider '{provider}'.\n"
            f"Set it in .env or PowerShell before running:\n"
            f'  $env:{key_name} = "YOUR_API_KEY"',
            file=sys.stderr,
        )
        return 1

    system_instruction = args.prompt
    if args.context:
        with open(args.context, "r", encoding="utf-8") as f:
            context_text = f.read().strip()
        system_instruction = f"{system_instruction}\n\n{context_text}"

    event_queue: queue.Queue[UIEvent] = queue.Queue()
    stop_event = threading.Event()
    pause_event = threading.Event()

    if provider == "gemini":
        worker = GeminiTeamsWorker(
            api_key=api_key,
            model=model,
            api_version=args.api_version,
            vertexai=args.vertexai,
            device_index=args.device,
            app_filter=None if args.all_audio else args.app,
            system_instruction=system_instruction,
            event_queue=event_queue,
            stop_event=stop_event,
            pause_event=pause_event,
        )
    else:
        worker = VadChatCopilotWorker(
            provider=provider,
            api_key=api_key,
            model=model,
            device_index=args.device,
            app_filter=None if args.all_audio else args.app,
            system_instruction=system_instruction,
            event_queue=event_queue,
            stop_event=stop_event,
            pause_event=pause_event,
        )
    worker.start()

    if args.ui in ("auto", "overlay"):
        try:
            ui = OverlayWindow(event_queue, stop_event, pause_event)
            ui.run()
        except tk.TclError as exc:
            if args.ui == "overlay":
                print(
                    f"Tkinter overlay unavailable ({exc}); falling back to Web UI.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    "Tkinter Tcl/Tk unavailable; opening Web UI in browser.",
                    flush=True,
                )
            run_web_ui(event_queue, stop_event, pause_event)
    elif args.ui == "web":
        run_web_ui(event_queue, stop_event, pause_event)
    else:
        run_terminal_ui(event_queue, stop_event)

    stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
