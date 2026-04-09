from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Dict, List, Optional
from uuid import uuid4

from models import ResolvedTrigger, SessionState, VoiceSession, utc_now
from settings import CameraProfile


class SessionManagerError(Exception):
    pass


class SessionManager:
    ACTIVE_STATES = {SessionState.ARMED, SessionState.LISTENING_FOR_WAKEWORD, SessionState.CAPTURING_COMMAND, SessionState.PROCESSING, SessionState.RESPONDING}

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger.getChild("session_manager")
        self._lock = asyncio.Lock()
        self._sessions_by_camera: Dict[str, VoiceSession] = {}
        self._sessions_by_id: Dict[str, VoiceSession] = {}

    async def create_session(self, trigger: ResolvedTrigger, profile: CameraProfile) -> VoiceSession:
        async with self._lock:
            existing = self._sessions_by_camera.get(profile.camera_id)
            if existing and existing.state in self.ACTIVE_STATES:
                raise SessionManagerError(f"Camera '{profile.camera_id}' already has an active session ('{existing.session_id}') in state '{existing.state.value}'.")
            session = VoiceSession(
                session_id=str(uuid4()), camera_id=profile.camera_id, camera_name=profile.camera_name, state=SessionState.ARMED,
                trigger_type=trigger.trigger_type, event_id=trigger.event_id, face_name=trigger.face_name, face_allowed=trigger.face_allowed,
                is_known_face=trigger.is_known_face, person_detected=trigger.person_detected, speech_detected=trigger.speech_detected,
                language=profile.language, agent_id=profile.agent_id, speaker_media_player=profile.speaker_media_player, tts_entity=profile.tts_entity,
                metadata={"wake_word": profile.wake_word, "session_open_seconds": profile.session_open_seconds, "require_known_face": profile.require_known_face, "require_person_presence": profile.require_person_presence, "person_sensor": profile.person_sensor, "response_enabled": profile.response_enabled, "save_audio_debug": profile.save_audio_debug},
            )
            session.touch()
            self._sessions_by_camera[profile.camera_id] = session
            self._sessions_by_id[session.session_id] = session
            self._logger.info("Session created. session_id=%s camera_id=%s face_name=%s", session.session_id, session.camera_id, session.face_name or "")
            return session

    async def set_state(self, session_id: str, state: SessionState) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            session.set_state(state)
            if state in {SessionState.CLOSED, SessionState.REJECTED, SessionState.COOLDOWN}:
                current = self._sessions_by_camera.get(session.camera_id)
                if current and current.session_id == session.session_id:
                    self._sessions_by_camera[session.camera_id] = session
            else:
                self._sessions_by_camera[session.camera_id] = session
            self._logger.info("Session state changed. session_id=%s camera_id=%s state=%s", session.session_id, session.camera_id, state.value)
            return session

    async def mark_listening(self, session_id: str) -> VoiceSession:
        return await self.set_state(session_id, SessionState.LISTENING_FOR_WAKEWORD)

    async def mark_capturing(self, session_id: str) -> VoiceSession:
        return await self.set_state(session_id, SessionState.CAPTURING_COMMAND)

    async def mark_processing(self, session_id: str) -> VoiceSession:
        return await self.set_state(session_id, SessionState.PROCESSING)

    async def mark_responding(self, session_id: str) -> VoiceSession:
        return await self.set_state(session_id, SessionState.RESPONDING)

    async def close_session(self, session_id: str, *, result: Optional[str] = None, rejection_reason: Optional[str] = None) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            if result is not None:
                session.result = result
            if rejection_reason is not None:
                session.rejection_reason = rejection_reason
            session.set_state(SessionState.CLOSED)
            current = self._sessions_by_camera.get(session.camera_id)
            if current and current.session_id == session.session_id:
                del self._sessions_by_camera[session.camera_id]
            self._logger.info("Session closed. session_id=%s camera_id=%s result=%s", session.session_id, session.camera_id, session.result or "")
            return session

    async def reject_session(self, session_id: str, *, reason: str) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            session.rejection_reason = reason
            session.result = "rejected"
            session.set_state(SessionState.REJECTED)
            current = self._sessions_by_camera.get(session.camera_id)
            if current and current.session_id == session.session_id:
                del self._sessions_by_camera[session.camera_id]
            self._logger.info("Session rejected. session_id=%s camera_id=%s reason=%s", session.session_id, session.camera_id, reason)
            return session

    async def attach_transcript(self, session_id: str, transcript: str) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            session.transcript = (transcript or "").strip() or None
            session.touch()
            return session

    async def attach_audio_clip(self, session_id: str, audio_clip: str) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            session.audio_clip = (audio_clip or "").strip() or None
            session.touch()
            return session

    async def attach_assistant_response(self, session_id: str, *, response_type: Optional[str], response_speech: Optional[str], success_targets: Optional[List[str]] = None, failed_targets: Optional[List[str]] = None) -> VoiceSession:
        async with self._lock:
            session = self._require_session(session_id)
            session.assistant_response_type = response_type
            session.assistant_response_speech = response_speech
            session.assistant_success_targets = list(success_targets or [])
            session.assistant_failed_targets = list(failed_targets or [])
            session.touch()
            return session

    async def cleanup_closed_sessions(self, keep_minutes: int = 30) -> int:
        if keep_minutes < 1:
            keep_minutes = 1
        cutoff = utc_now() - timedelta(minutes=keep_minutes)
        deleted: List[str] = []
        async with self._lock:
            for session_id, session in list(self._sessions_by_id.items()):
                if session.state not in {SessionState.CLOSED, SessionState.REJECTED, SessionState.COOLDOWN}:
                    continue
                ref = session.closed_at_utc or session.last_activity_at_utc or session.created_at_utc
                if ref < cutoff:
                    deleted.append(session_id)
                    del self._sessions_by_id[session_id]
                    current = self._sessions_by_camera.get(session.camera_id)
                    if current and current.session_id == session_id:
                        del self._sessions_by_camera[session.camera_id]
        if deleted:
            self._logger.info("Removed %s old sessions from memory.", len(deleted))
        return len(deleted)

    def _require_session(self, session_id: str) -> VoiceSession:
        session = self._sessions_by_id.get(session_id)
        if session is None:
            raise SessionManagerError(f"Session '{session_id}' was not found.")
        return session
