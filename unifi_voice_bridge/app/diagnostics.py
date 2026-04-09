from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from protect_client import ProtectClient
from settings import AppSettings, CameraProfile


class DiagnosticsService:
    def __init__(self, settings: AppSettings, logger: logging.Logger, *, protect_client: ProtectClient) -> None:
        self._settings = settings
        self._logger = logger.getChild("diagnostics")
        self._protect = protect_client

    async def get_camera_diagnostics(self) -> List[Dict[str, Any]]:
        protect_camera_map = await self._protect.get_camera_map(force=False)
        result: List[Dict[str, Any]] = []
        for profile in self._settings.cameras:
            resolved_rtsp: Optional[str] = None
            rtsp_error: Optional[str] = None
            try:
                if profile.rtsp_source:
                    resolved_rtsp = profile.rtsp_source
                else:
                    resolved_rtsp = await self._protect.get_camera_audio_stream_url(profile.camera_id, prefer_secure=self._settings.addon.rtsp_prefer_secure)
            except Exception as exc:
                rtsp_error = str(exc)
            result.append({"camera_id": profile.camera_id, "camera_name": profile.camera_name, "enabled": profile.enabled, "configured_in_profiles": True, "found_in_protect": profile.camera_id in protect_camera_map, "protect_camera_name": protect_camera_map.get(profile.camera_id), "allowed_faces": list(profile.allowed_faces), "speaker_media_player": profile.speaker_media_player, "tts_entity": profile.tts_entity, "rtsp_override": profile.rtsp_source, "resolved_rtsp_source": self._mask_rtsp_source(resolved_rtsp), "resolved_rtsp_error": rtsp_error, "require_known_face": profile.require_known_face, "require_person_presence": profile.require_person_presence, "person_sensor": profile.person_sensor, "response_enabled": profile.response_enabled, "save_audio_debug": profile.save_audio_debug, "wake_word": profile.wake_word, "language": profile.language, "agent_id": profile.agent_id})
        return result

    async def get_runtime_summary(self) -> Dict[str, Any]:
        protect_camera_map = await self._protect.get_camera_map(force=False)
        enabled = [c for c in self._settings.cameras if c.enabled]
        disabled = [c for c in self._settings.cameras if not c.enabled]
        return {"protect_host": self._settings.addon.protect_host, "protect_port": self._settings.addon.protect_port, "configured_camera_count": len(self._settings.cameras), "enabled_camera_count": len(enabled), "disabled_camera_count": len(disabled), "protect_camera_count": len(protect_camera_map), "test_mode_enabled": self._settings.addon.test_mode_enabled, "test_mode_record_seconds": self._settings.addon.test_mode_record_seconds, "wakeword_enabled": self._settings.addon.wakeword_enabled, "stt_engine": self._settings.addon.stt_engine, "rtsp_transport": self._settings.addon.rtsp_transport, "rtsp_prefer_secure": self._settings.addon.rtsp_prefer_secure}

    async def get_rtsp_self_check_summary(self) -> Dict[str, Any]:
        cameras = await self.get_camera_diagnostics()
        enabled = [c for c in cameras if c.get("enabled")]
        success = [c for c in enabled if c.get("resolved_rtsp_source") and not c.get("resolved_rtsp_error")]
        failed = [c for c in enabled if not c.get("resolved_rtsp_source") or c.get("resolved_rtsp_error")]
        return {"checked_camera_count": len(enabled), "success_count": len(success), "failure_count": len(failed), "successful_cameras": [{"camera_id": c.get("camera_id"), "camera_name": c.get("camera_name"), "resolved_rtsp_source": c.get("resolved_rtsp_source")} for c in success], "failed_cameras": [{"camera_id": c.get("camera_id"), "camera_name": c.get("camera_name"), "resolved_rtsp_source": c.get("resolved_rtsp_source"), "resolved_rtsp_error": c.get("resolved_rtsp_error")} for c in failed]}

    def get_camera_profile(self, camera_id: str) -> Optional[CameraProfile]:
        camera_id = (camera_id or "").strip()
        if not camera_id:
            return None
        return self._settings.camera_map.get(camera_id)

    def _mask_rtsp_source(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        text = value.strip()
        if "@" in text:
            prefix, suffix = text.split("@", 1)
            if "://" in prefix:
                scheme, _ = prefix.split("://", 1)
                return f"{scheme}://***@{suffix}"
        return text
