from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from audio_stream import AudioStreamError, RtspAudioStream
from log_store import LogStore
from models import ResolvedTrigger, VoiceSession, WebhookEvent
from protect_client import ProtectClient, ProtectClientError
from session_manager import SessionManager, SessionManagerError
from settings import AppSettings, CameraProfile


class TestModeRecorder:
    def __init__(self, settings: AppSettings, logger: logging.Logger, *, protect_client: ProtectClient, session_manager: SessionManager, log_store: LogStore) -> None:
        self._settings = settings
        self._logger = logger.getChild("test_mode")
        self._protect = protect_client
        self._sessions = session_manager
        self._log_store = log_store

    async def process_trigger(self, trigger: ResolvedTrigger, profile: CameraProfile) -> VoiceSession:
        return await self._record_session(trigger=trigger, profile=profile, reason="authorized_trigger")

    async def process_manual_test(self, profile: CameraProfile) -> VoiceSession:
        synthetic_event = WebhookEvent(raw_payload={"manual_test": True, "camera_id": profile.camera_id}, received_at_utc=datetime.now(timezone.utc), trigger_type="manual_test", camera_id=profile.camera_id, camera_name=profile.camera_name, event_id=None, face_name="manual_test", is_known_face=True, person_detected=True, speech_detected=None)
        synthetic_trigger = ResolvedTrigger(accepted=True, reason=None, trigger_type="manual_test", camera_id=profile.camera_id, camera_name=profile.camera_name, face_name="manual_test", face_allowed=True, is_known_face=True, person_detected=True, speech_detected=None, event_id=None, webhook_event=synthetic_event)
        return await self._record_session(trigger=synthetic_trigger, profile=profile, reason="manual_test")

    async def _record_session(self, *, trigger: ResolvedTrigger, profile: CameraProfile, reason: str) -> VoiceSession:
        session = await self._sessions.create_session(trigger, profile)
        stream: Optional[RtspAudioStream] = None
        try:
            rtsp_source = await self._resolve_rtsp_source(profile)
            if not rtsp_source:
                session.metadata["error"] = "rtsp_source_not_found"
                session.metadata["mode"] = reason
                session = await self._sessions.reject_session(session.session_id, reason="rtsp_source_not_found")
                self._log_store.append_session(session)
                return session
            session.metadata["rtsp_source"] = self._mask_rtsp_source(rtsp_source)
            session.metadata["test_mode"] = True
            session.metadata["mode"] = reason
            session.metadata["test_mode_record_seconds"] = self._settings.addon.test_mode_record_seconds
            stream = RtspAudioStream(rtsp_source, self._logger, rtsp_transport=self._settings.addon.rtsp_transport)
            await self._sessions.mark_capturing(session.session_id)
            clip_path = self._log_store.reserve_audio_clip_path(session.camera_id, session.session_id)
            recorded = await stream.record_to_wav(clip_path, duration_seconds=float(self._settings.addon.test_mode_record_seconds))
            await self._sessions.attach_audio_clip(session.session_id, str(recorded))
            session = await self._sessions.close_session(session.session_id, result="test_recorded")
            self._log_store.append_session(session)
            self._logger.info("Test mode recording complete. session_id=%s camera_id=%s clip=%s mode=%s", session.session_id, session.camera_id, recorded, reason)
            return session
        except (ProtectClientError, AudioStreamError, SessionManagerError) as exc:
            self._logger.exception("Test mode failed: %s", exc)
            session.metadata["error"] = str(exc)
            session.metadata["mode"] = reason
            session = await self._sessions.close_session(session.session_id, result="test_mode_error")
            self._log_store.append_session(session)
            return session
        except Exception as exc:
            self._logger.exception("Unhandled test mode error: %s", exc)
            session.metadata["error"] = str(exc)
            session.metadata["mode"] = reason
            session = await self._sessions.close_session(session.session_id, result="test_mode_error")
            self._log_store.append_session(session)
            return session
        finally:
            if stream is not None:
                await stream.stop()

    async def _resolve_rtsp_source(self, profile: CameraProfile) -> Optional[str]:
        if profile.rtsp_source:
            return profile.rtsp_source
        return await self._protect.get_camera_audio_stream_url(profile.camera_id, prefer_secure=self._settings.addon.rtsp_prefer_secure)

    def _mask_rtsp_source(self, value: str) -> str:
        text = (value or "").strip()
        if "@" in text:
            prefix, suffix = text.split("@", 1)
            if "://" in prefix:
                scheme, _ = prefix.split("://", 1)
                return f"{scheme}://***@{suffix}"
        return text
