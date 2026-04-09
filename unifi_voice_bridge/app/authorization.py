from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from models import ResolvedTrigger, WebhookEvent
from settings import AppSettings, CameraProfile


class AuthorizationService:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger.getChild("authorization")
        self._last_accept_by_camera: Dict[str, float] = {}

    async def authorize_event(self, event: WebhookEvent) -> ResolvedTrigger:
        camera_profile = self._resolve_camera_profile(event)
        if camera_profile is None:
            return self._reject(event=event, reason="camera_not_configured", camera_id=event.camera_id or "unknown", camera_name=event.camera_name or event.camera_id or "Unknown Camera", face_allowed=False)
        if not camera_profile.enabled:
            return self._reject(event=event, reason="camera_disabled", camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=False)
        cooldown_reason = self._check_cooldown(camera_profile)
        if cooldown_reason:
            return self._reject(event=event, reason=cooldown_reason, camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=self._is_face_allowed(camera_profile, event.face_name))
        if camera_profile.require_person_presence and event.person_detected is False:
            return self._reject(event=event, reason="person_not_detected", camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=self._is_face_allowed(camera_profile, event.face_name))
        if camera_profile.require_known_face and event.is_known_face is False:
            return self._reject(event=event, reason="face_not_known", camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=False)
        if camera_profile.require_known_face and not event.face_name:
            return self._reject(event=event, reason="face_name_missing", camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=False)
        face_allowed = self._is_face_allowed(camera_profile, event.face_name)
        if not face_allowed:
            return self._reject(event=event, reason="face_not_in_allow_list", camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_allowed=False)
        self._last_accept_by_camera[camera_profile.camera_id] = time.monotonic()
        self._logger.info("Accepted trigger. camera_id=%s camera_name=%s face_name=%s trigger_type=%s", camera_profile.camera_id, camera_profile.camera_name, event.face_name or "", event.trigger_type)
        return ResolvedTrigger(accepted=True, reason=None, trigger_type=event.trigger_type, camera_id=camera_profile.camera_id, camera_name=camera_profile.camera_name, face_name=event.face_name, face_allowed=True, is_known_face=event.is_known_face, person_detected=event.person_detected, speech_detected=event.speech_detected, event_id=event.event_id, webhook_event=event)

    def _resolve_camera_profile(self, event: WebhookEvent) -> Optional[CameraProfile]:
        if event.camera_id:
            profile = self._settings.camera_map.get(event.camera_id)
            if profile is not None:
                return profile
        if event.camera_name:
            wanted = event.camera_name.casefold().strip()
            for profile in self._settings.cameras:
                if profile.camera_name.casefold().strip() == wanted:
                    return profile
        return None

    def _check_cooldown(self, profile: CameraProfile) -> Optional[str]:
        seconds = profile.cooldown_seconds
        if seconds <= 0:
            return None
        previous = self._last_accept_by_camera.get(profile.camera_id)
        if previous is None:
            return None
        elapsed = time.monotonic() - previous
        if elapsed < seconds:
            return f"camera_cooldown_active:{round(seconds - elapsed, 1)}s"
        return None

    def _is_face_allowed(self, profile: CameraProfile, face_name: Optional[str]) -> bool:
        if not profile.allowed_faces:
            return True
        if not face_name:
            return False
        wanted = face_name.casefold().strip()
        return any(x.casefold().strip() == wanted for x in profile.allowed_faces)

    def _reject(self, *, event: WebhookEvent, reason: str, camera_id: str, camera_name: str, face_allowed: bool) -> ResolvedTrigger:
        self._logger.info("Rejected trigger. reason=%s camera_id=%s camera_name=%s face_name=%s trigger_type=%s", reason, camera_id, camera_name, event.face_name or "", event.trigger_type)
        return ResolvedTrigger(accepted=False, reason=reason, trigger_type=event.trigger_type, camera_id=camera_id, camera_name=camera_name, face_name=event.face_name, face_allowed=face_allowed, is_known_face=event.is_known_face, person_detected=event.person_detected, speech_detected=event.speech_detected, event_id=event.event_id, webhook_event=event)
