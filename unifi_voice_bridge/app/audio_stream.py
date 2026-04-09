from __future__ import annotations

import asyncio
import logging
import shlex
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional


class AudioStreamError(Exception):
    pass


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int = 16000
    channels: int = 1
    sample_width_bytes: int = 2

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width_bytes


class RtspAudioStream:
    def __init__(self, source_url: str, logger: logging.Logger, *, rtsp_transport: str = "tcp", audio_format: Optional[AudioFormat] = None, chunk_ms: int = 20, ffmpeg_bin: str = "ffmpeg") -> None:
        self._source_url = (source_url or "").strip()
        self._logger = logger.getChild("audio_stream")
        self._rtsp_transport = (rtsp_transport or "tcp").strip() or "tcp"
        self._format = audio_format or AudioFormat()
        self._chunk_ms = max(10, chunk_ms)
        self._ffmpeg_bin = ffmpeg_bin
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def chunk_size(self) -> int:
        return max(1, int(self._format.bytes_per_second * (self._chunk_ms / 1000.0)))

    async def start(self) -> None:
        if not self._source_url:
            raise AudioStreamError("Audio source URL is empty.")
        if self._process is not None and self._process.returncode is None:
            return
        cmd = [self._ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-nostdin", "-rtsp_transport", self._rtsp_transport, "-i", self._source_url, "-vn", "-acodec", "pcm_s16le", "-ac", str(self._format.channels), "-ar", str(self._format.sample_rate), "-f", "s16le", "pipe:1"]
        self._logger.info("Starting audio stream: %s", " ".join(shlex.quote(x) for x in cmd[:-1]) + " pipe:1")
        try:
            self._process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as exc:
            raise AudioStreamError(f"Could not find ffmpeg binary '{self._ffmpeg_bin}'.") from exc
        except Exception as exc:
            raise AudioStreamError(f"Could not start ffmpeg audio stream: {exc}") from exc
        self._closed = False
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="audio_stream_stderr")

    async def iter_pcm_chunks(self) -> AsyncIterator[bytes]:
        await self.start()
        if self._process is None or self._process.stdout is None:
            raise AudioStreamError("Audio process stdout is not available.")
        try:
            while not self._closed:
                chunk = await self._process.stdout.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await self.stop()

    async def record_to_wav(self, output_path: Path, duration_seconds: float) -> Path:
        if duration_seconds <= 0:
            raise AudioStreamError("duration_seconds must be greater than zero.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(self._format.bytes_per_second * duration_seconds)
        written = 0
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(self._format.channels)
            wf.setsampwidth(self._format.sample_width_bytes)
            wf.setframerate(self._format.sample_rate)
            async for chunk in self.iter_pcm_chunks():
                if written >= max_bytes:
                    break
                remaining = max_bytes - written
                to_write = chunk[:remaining]
                if not to_write:
                    break
                wf.writeframes(to_write)
                written += len(to_write)
                if written >= max_bytes:
                    break
        self._logger.info("Audio recording saved. path=%s bytes=%s approx_seconds=%.2f", output_path, written, written / float(self._format.bytes_per_second))
        return output_path

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            if self._process.returncode is None:
                self._process.kill()
                try:
                    await self._process.wait()
                except Exception:
                    pass
            self._process = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._stderr_task = None

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._logger.debug("ffmpeg: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.debug("Audio stderr reader stopped: %s", exc)
