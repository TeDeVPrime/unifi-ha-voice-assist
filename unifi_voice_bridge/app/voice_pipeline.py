from __future__ import annotations

import audioop
import logging
import wave
from pathlib import Path
from typing import AsyncIterator, Optional

from audio_stream import AudioStreamError, RtspAudioStream
from ha_client import HomeAssistantClient, HomeAssistantClientError
from log_store import LogStore
from models import ResolvedTrigger, VoiceSession
from protect_client import ProtectClient, ProtectClientError
from session_manager import SessionManager, SessionManagerError
from settings import AppSettings, CameraProfile
from stt import SpeechToTextError, SpeechToTextService
from wakeword import WakeWordError, WakeWordService


class VoicePipeline:
    def __init__(self, settings: AppSettings, logger: logging.Logger, *, protect_client: ProtectClient, session_manager: SessionManager, stt_service: SpeechToTextService, wakeword_service: WakeWordService, ha_client: HomeAssistantClient, log_store: LogStore) -> None:
        self._settings = settings
        self._logger = logger.getChild("voice_pipeline")
        self._protect = protect_client
        self._sessions = session_manager
        self._stt = stt_service
        self._wakeword = wakeword_service
        self._ha = ha_client
        self._log_store = log_store

    async def process_trigger(self, trigger: ResolvedTrigger, profile: CameraProfile) -> VoiceSession:
        session = await self._sessions.create_session(trigger, profile)
        stream: Optional[RtspAudioStream] = None
        try:
            rtsp_source = await self._resolve_rtsp_source(profile)
            if not rtsp_source:
                session.metadata["error"] = "rtsp_source_not_found"
                session = await self._sessions.reject_session(session.session_id, reason="rtsp_source_not_found")
                self._log_store.append_session(session)
                return session
            session.metadata["rtsp_source"] = self._mask_rtsp_source(rtsp_source)
            stream = RtspAudioStream(rtsp_source, self._logger, rtsp_transport=self._settings.addon.rtsp_transport)
            audio_iter = stream.iter_pcm_chunks()
            await self._sessions.mark_listening(session.session_id)
            wake_result = await self._wakeword.wait_for_wakeword(audio_iter, timeout_seconds=profile.session_open_seconds, expected_wake_word=profile.wake_word, score_threshold=self._settings.addon.wakeword_score_threshold_pct / 100.0)
            session.metadata["wakeword"] = {"detected": wake_result.detected, "reason": wake_result.reason, "wake_word": wake_result.wake_word, "score": wake_result.score, "detected_at_seconds": wake_result.detected_at_seconds}
            if not wake_result.detected:
                session = await self._sessions.close_session(session.session_id, result="wakeword_not_detected")
                self._log_store.append_session(session)
                return session
            await self._sessions.mark_capturing(session.session_id)
            command_pcm = await self._capture_command_audio(audio_iter, initial_audio=wake_result.initial_command_audio, max_seconds=self._settings.addon.command_max_ms / 1000.0, silence_timeout_seconds=self._settings.addon.silence_timeout_ms / 1000.0, minimum_command_seconds=self._settings.addon.minimum_command_ms / 1000.0, silence_rms_threshold=self._settings.addon.silence_rms_threshold)
            if not command_pcm:
                session = await self._sessions.close_session(session.session_id, result="command_audio_empty")
                self._log_store.append_session(session)
                return session
            if profile.save_audio_debug:
                clip_path = self._log_store.reserve_audio_clip_path(session.camera_id, session.session_id)
                self._write_pcm_to_wav(clip_path, command_pcm)
                await self._sessions.attach_audio_clip(session.session_id, str(clip_path))
            await self._sessions.mark_processing(session.session_id)
            transcript_result = await self._stt.transcribe_pcm_bytes_to_temp_wav(command_pcm, sample_rate=16000, channels=1, sample_width_bytes=2, language=session.language)
            session.metadata["stt"] = {"engine": transcript_result.engine, "model": transcript_result.model, "language": transcript_result.language, "duration_seconds": transcript_result.duration_seconds, "segment_count": len(transcript_result.segments)}
            transcript_text = (transcript_result.text or "").strip()
            await self._sessions.attach_transcript(session.session_id, transcript_text)
            if not transcript_text:
                session = await self._sessions.close_session(session.session_id, result="transcript_empty")
                self._log_store.append_session(session)
                return session
            conversation_result = await self._ha.process_conversation(transcript_text, language=session.language, agent_id=session.agent_id)
            await self._sessions.attach_assistant_response(session.session_id, response_type=conversation_result.response_type, response_speech=conversation_result.response_speech, success_targets=conversation_result.success_targets, failed_targets=conversation_result.failed_targets)
            final_result = "conversation_processed"
            if profile.response_enabled and session.tts_entity and session.speaker_media_player and conversation_result.response_speech:
                await self._sessions.mark_responding(session.session_id)
                try:
                    await self._ha.speak(tts_entity=session.tts_entity, media_player_entity_id=session.speaker_media_player, message=conversation_result.response_speech, language=conversation_result.response_language or session.language)
                    final_result = "success"
                except HomeAssistantClientError as exc:
                    session.metadata["tts_error"] = str(exc)
                    final_result = "conversation_ok_tts_failed"
            else:
                final_result = "success_without_tts"
            session = await self._sessions.close_session(session.session_id, result=final_result)
            self._log_store.append_session(session)
            return session
        except (ProtectClientError, AudioStreamError, WakeWordError, SpeechToTextError, SessionManagerError, HomeAssistantClientError) as exc:
            self._logger.exception("Voice pipeline failed: %s", exc)
            session.metadata["error"] = str(exc)
            session = await self._sessions.close_session(session.session_id, result="pipeline_error")
            self._log_store.append_session(session)
            return session
        except Exception as exc:
            self._logger.exception("Unhandled voice pipeline error: %s", exc)
            session.metadata["error"] = str(exc)
            session = await self._sessions.close_session(session.session_id, result="pipeline_error")
            self._log_store.append_session(session)
            return session
        finally:
            if stream is not None:
                await stream.stop()

    async def _resolve_rtsp_source(self, profile: CameraProfile) -> Optional[str]:
        if profile.rtsp_source:
            return profile.rtsp_source
        return await self._protect.get_camera_audio_stream_url(profile.camera_id, prefer_secure=self._settings.addon.rtsp_prefer_secure)

    async def _capture_command_audio(self, audio_iter: AsyncIterator[bytes], *, initial_audio: bytes, max_seconds: float, silence_timeout_seconds: float, minimum_command_seconds: float, sample_rate: int = 16000, channels: int = 1, sample_width_bytes: int = 2, silence_rms_threshold: int = 450) -> bytes:
        bytes_per_second = sample_rate * channels * sample_width_bytes
        max_bytes = int(bytes_per_second * max_seconds)
        silence_limit_bytes = int(bytes_per_second * silence_timeout_seconds)
        minimum_bytes = int(bytes_per_second * minimum_command_seconds)
        collected = bytearray()
        silence_run = 0
        heard_non_silent = False
        if initial_audio:
            collected.extend(initial_audio)
            if not self._is_silent(initial_audio, sample_width_bytes, silence_rms_threshold):
                heard_non_silent = True
        while len(collected) < max_bytes:
            try:
                chunk = await audio_iter.__anext__()
            except StopAsyncIteration:
                break
            if not chunk:
                continue
            remaining = max_bytes - len(collected)
            if remaining <= 0:
                break
            chunk = chunk[:remaining]
            collected.extend(chunk)
            silent = self._is_silent(chunk, sample_width_bytes, silence_rms_threshold)
            if silent:
                silence_run += len(chunk)
            else:
                heard_non_silent = True
                silence_run = 0
            if heard_non_silent and len(collected) >= minimum_bytes and silence_run >= silence_limit_bytes:
                break
        return bytes(collected)

    def _is_silent(self, pcm_chunk: bytes, sample_width_bytes: int, rms_threshold: int) -> bool:
        if not pcm_chunk:
            return True
        try:
            rms = audioop.rms(pcm_chunk, sample_width_bytes)
            return rms < rms_threshold
        except Exception:
            return False

    def _write_pcm_to_wav(self, output_path: Path, pcm_bytes: bytes, *, sample_rate: int = 16000, channels: int = 1, sample_width_bytes: int = 2) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width_bytes)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return output_path

    def _mask_rtsp_source(self, value: str) -> str:
        text = (value or "").strip()
        if "@" in text:
            prefix, suffix = text.split("@", 1)
            if "://" in prefix:
                scheme, _ = prefix.split("://", 1)
                return f"{scheme}://***@{suffix}"
        return text
