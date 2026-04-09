from __future__ import annotations

import tempfile
import wave
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from settings import AppSettings


class SpeechToTextError(Exception):
    pass


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: Optional[str]
    engine: str
    model: str
    duration_seconds: Optional[float]
    segments: List[TranscriptSegment] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SpeechToTextService:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger.getChild("stt")
        self._engine = settings.addon.stt_engine
        self._model_name = settings.addon.stt_model
        self._model: Any = None

    async def start(self) -> None:
        if self._engine == "external":
            self._logger.info("STT engine is set to external. Local STT model not loaded.")
            return
        if self._model is not None:
            return
        if self._engine == "faster_whisper":
            self._model = self._load_faster_whisper_model()
            self._logger.info("Loaded faster-whisper model '%s'.", self._model_name)
            return
        if self._engine == "whisper":
            self._model = self._load_whisper_model()
            self._logger.info("Loaded whisper model '%s'.", self._model_name)
            return
        raise SpeechToTextError(f"Unsupported STT engine '{self._engine}'.")

    async def transcribe_wav(self, wav_path: Path, *, language: Optional[str] = None) -> TranscriptResult:
        path = Path(wav_path)
        if not path.exists():
            raise SpeechToTextError(f"Audio file not found: {path}")
        duration_seconds = _get_wav_duration_seconds(path)
        if self._engine == "external":
            raise SpeechToTextError("STT engine is set to 'external', but no external STT adapter has been implemented yet.")
        await self.start()
        if self._engine == "faster_whisper":
            return self._transcribe_with_faster_whisper(path, language=language, duration_seconds=duration_seconds)
        if self._engine == "whisper":
            return self._transcribe_with_whisper(path, language=language, duration_seconds=duration_seconds)
        raise SpeechToTextError(f"Unsupported STT engine '{self._engine}'.")

    async def transcribe_pcm_bytes_to_temp_wav(self, pcm_bytes: bytes, *, sample_rate: int = 16000, channels: int = 1, sample_width_bytes: int = 2, language: Optional[str] = None) -> TranscriptResult:
        if not pcm_bytes:
            raise SpeechToTextError("PCM input is empty.")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with wave.open(str(temp_path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sample_width_bytes)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)
            return await self.transcribe_wav(temp_path, language=language)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _load_faster_whisper_model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise SpeechToTextError("Could not import faster_whisper. Install it in the add-on image or switch stt_engine.") from exc
        try:
            return WhisperModel(self._model_name, device=self._settings.addon.stt_device, compute_type=self._settings.addon.stt_compute_type)
        except Exception as exc:
            raise SpeechToTextError(f"Could not load faster-whisper model '{self._model_name}': {exc}") from exc

    def _load_whisper_model(self) -> Any:
        try:
            import whisper
        except Exception as exc:
            raise SpeechToTextError("Could not import whisper. Install it in the add-on image or switch stt_engine.") from exc
        try:
            return whisper.load_model(self._model_name)
        except Exception as exc:
            raise SpeechToTextError(f"Could not load whisper model '{self._model_name}': {exc}") from exc

    def _transcribe_with_faster_whisper(self, wav_path: Path, *, language: Optional[str], duration_seconds: Optional[float]) -> TranscriptResult:
        if self._model is None:
            raise SpeechToTextError("faster-whisper model is not loaded.")
        try:
            segments_iter, info = self._model.transcribe(str(wav_path), language=language)
            segments = list(segments_iter)
        except Exception as exc:
            raise SpeechToTextError(f"faster-whisper transcription failed: {exc}") from exc
        clean_segments: List[TranscriptSegment] = []
        text_parts: List[str] = []
        for segment in segments:
            seg_text = str(getattr(segment, "text", "")).strip()
            if seg_text:
                text_parts.append(seg_text)
            clean_segments.append(TranscriptSegment(start=float(getattr(segment, "start", 0.0)), end=float(getattr(segment, "end", 0.0)), text=seg_text))
        detected_language = getattr(info, "language", None)
        full_text = " ".join(x for x in text_parts if x).strip()
        self._logger.info("STT complete with faster-whisper. language=%s text_length=%s", detected_language or "", len(full_text))
        return TranscriptResult(text=full_text, language=detected_language or language, engine="faster_whisper", model=self._model_name, duration_seconds=duration_seconds, segments=clean_segments, metadata={"language_probability": getattr(info, "language_probability", None)})

    def _transcribe_with_whisper(self, wav_path: Path, *, language: Optional[str], duration_seconds: Optional[float]) -> TranscriptResult:
        if self._model is None:
            raise SpeechToTextError("whisper model is not loaded.")
        try:
            result = self._model.transcribe(str(wav_path), language=language)
        except Exception as exc:
            raise SpeechToTextError(f"whisper transcription failed: {exc}") from exc
        segments_raw = result.get("segments") if isinstance(result, dict) else None
        clean_segments: List[TranscriptSegment] = []
        if isinstance(segments_raw, list):
            for segment in segments_raw:
                if not isinstance(segment, dict):
                    continue
                clean_segments.append(TranscriptSegment(start=float(segment.get("start", 0.0)), end=float(segment.get("end", 0.0)), text=str(segment.get("text", "")).strip()))
        full_text = ""
        detected_language: Optional[str] = None
        if isinstance(result, dict):
            full_text = str(result.get("text", "")).strip()
            detected_language = str(result.get("language", "")).strip() or None
        self._logger.info("STT complete with whisper. language=%s text_length=%s", detected_language or "", len(full_text))
        return TranscriptResult(text=full_text, language=detected_language or language, engine="whisper", model=self._model_name, duration_seconds=duration_seconds, segments=clean_segments, metadata={})


def _get_wav_duration_seconds(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except Exception:
        return None
