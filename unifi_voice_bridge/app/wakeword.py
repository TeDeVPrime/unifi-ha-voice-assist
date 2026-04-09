from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from settings import AppSettings


class WakeWordError(Exception):
    pass


@dataclass(frozen=True)
class WakeWordDetection:
    detected: bool
    reason: str
    wake_word: Optional[str]
    score: float
    detected_at_seconds: Optional[float]
    initial_command_audio: bytes = b""


class WakeWordService:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger.getChild("wakeword")
        self._enabled = settings.addon.wakeword_enabled
        self._model_path = (settings.addon.wakeword_model or "").strip()
        self._model: Any = None
        self._numpy: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._enabled:
            self._logger.info("Wake word detection is disabled.")
            return
        if self._model is not None:
            return
        self._numpy = self._import_numpy()
        self._model = self._load_model()
        self._logger.info("Wake word model loaded from '%s'.", self._model_path)

    async def wait_for_wakeword(self, audio_iter: AsyncIterator[bytes], *, timeout_seconds: float, expected_wake_word: Optional[str] = None, score_threshold: float = 0.5, sample_rate: int = 16000, channels: int = 1, sample_width_bytes: int = 2) -> WakeWordDetection:
        if not self._enabled:
            return WakeWordDetection(detected=True, reason="wakeword_disabled", wake_word=expected_wake_word, score=1.0, detected_at_seconds=0.0, initial_command_audio=b"")
        await self.start()
        if self._model is None or self._numpy is None:
            raise WakeWordError("Wake word model is not initialized.")
        bytes_per_second = sample_rate * channels * sample_width_bytes
        elapsed_seconds = 0.0
        async with self._lock:
            self._reset_model()
            while elapsed_seconds < timeout_seconds:
                remaining = max(0.1, timeout_seconds - elapsed_seconds)
                try:
                    chunk = await asyncio.wait_for(audio_iter.__anext__(), timeout=min(1.5, remaining + 0.25))
                except StopAsyncIteration:
                    return WakeWordDetection(detected=False, reason="stream_ended", wake_word=expected_wake_word, score=0.0, detected_at_seconds=elapsed_seconds, initial_command_audio=b"")
                except asyncio.TimeoutError:
                    return WakeWordDetection(detected=False, reason="timeout", wake_word=expected_wake_word, score=0.0, detected_at_seconds=elapsed_seconds, initial_command_audio=b"")
                if not chunk:
                    continue
                elapsed_seconds += len(chunk) / float(bytes_per_second)
                scores = self._predict_scores(chunk)
                match_name, match_score = self._pick_best_match(scores, expected_wake_word)
                if match_score >= score_threshold:
                    self._logger.info("Wake word detected. name=%s score=%.3f elapsed=%.2fs", match_name or "", match_score, elapsed_seconds)
                    return WakeWordDetection(detected=True, reason="detected", wake_word=match_name or expected_wake_word, score=match_score, detected_at_seconds=elapsed_seconds, initial_command_audio=chunk)
        return WakeWordDetection(detected=False, reason="timeout", wake_word=expected_wake_word, score=0.0, detected_at_seconds=elapsed_seconds, initial_command_audio=b"")

    def _import_numpy(self) -> Any:
        try:
            import numpy as np
            return np
        except Exception as exc:
            raise WakeWordError("Could not import numpy. Install it in the add-on image before using wake-word detection.") from exc

    def _load_model(self) -> Any:
        try:
            from openwakeword.model import Model
        except Exception as exc:
            raise WakeWordError("Could not import openwakeword. Install it in the add-on image before using wake-word detection.") from exc
        if not self._model_path:
            raise WakeWordError("wakeword_model is empty.")
        model_path = Path(self._model_path)
        if not model_path.exists():
            raise WakeWordError(f"Wake-word model file was not found: {model_path}")
        try:
            return Model(wakeword_models=[str(model_path)])
        except Exception as exc:
            raise WakeWordError(f"Could not load wake-word model '{model_path}': {exc}") from exc

    def _reset_model(self) -> None:
        if self._model is None:
            return
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()

    def _predict_scores(self, pcm_chunk: bytes) -> Dict[str, float]:
        if self._model is None or self._numpy is None:
            raise WakeWordError("Wake word model is not ready.")
        samples = self._numpy.frombuffer(pcm_chunk, dtype=self._numpy.int16)
        if samples.size == 0:
            return {}
        try:
            raw = self._model.predict(samples)
        except Exception as exc:
            raise WakeWordError(f"Wake-word prediction failed: {exc}") from exc
        return self._normalize_scores(raw)

    def _normalize_scores(self, raw: Any) -> Dict[str, float]:
        result: Dict[str, float] = {}
        if not isinstance(raw, dict):
            return result
        for key, value in raw.items():
            score: Optional[float] = None
            if isinstance(value, (int, float)):
                score = float(value)
            elif isinstance(value, dict):
                for score_key in ("score", "probability", "confidence"):
                    if score_key in value and isinstance(value[score_key], (int, float)):
                        score = float(value[score_key])
                        break
            if score is not None:
                result[str(key).strip()] = score
        return result

    def _pick_best_match(self, scores: Dict[str, float], expected_wake_word: Optional[str]) -> tuple[Optional[str], float]:
        if not scores:
            return None, 0.0
        if expected_wake_word:
            wanted = expected_wake_word.casefold().strip()
            for key, value in scores.items():
                normalized = key.casefold().strip()
                if normalized == wanted:
                    return key, value
            for key, value in scores.items():
                normalized = key.casefold().strip()
                if wanted in normalized or normalized in wanted:
                    return key, value
        best_name = None
        best_score = 0.0
        for key, value in scores.items():
            if value > best_score:
                best_name = key
                best_score = value
        return best_name, best_score
