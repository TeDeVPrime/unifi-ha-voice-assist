from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, List, Optional

from models import WebhookEvent
from protect_client import ProtectClient


class EventResolver:
    def __init__(self, protect_client: ProtectClient, logger: logging.Logger) -> None:
        self._protect = protect_client
        self._logger = logger.getChild("event_resolver")

    async def enrich_event(self, event: WebhookEvent) -> WebhookEvent:
        camera_id = event.camera_id
        camera_name = event.camera_name
        face_name = event.face_name
        is_known_face = event.is_known_face
        person_detected = event.person_detected
        speech_detected = event.speech_detected
        try:
            camera_map = await self._protect.get_camera_map(force=False)
        except Exception as exc:
            self._logger.warning("Could not load Protect camera map for event enrichment: %s", exc)
            camera_map = {}
        if camera_id and not camera_name:
            camera_name = camera_map.get(camera_id)
        if not camera_id and camera_name:
            resolved_camera_id = self._find_camera_id_by_name(camera_map, camera_name)
            if resolved_camera_id:
                camera_id = resolved_camera_id
                camera_name = camera_map.get(camera_id, camera_name)
        needs_recent_lookup = any(value is None or value == "" for value in (camera_name, face_name, is_known_face, person_detected, speech_detected))
        if camera_id and needs_recent_lookup:
            try:
                recent = await self._protect.get_recent_events(camera_id=camera_id, seconds_back=20, event_types=["smartDetect", "smartDetectZone"], limit=20)
            except Exception as exc:
                self._logger.warning("Could not load recent Protect events for enrichment. camera_id=%s error=%s", camera_id, exc)
                recent = []
            if recent:
                best = self._pick_best_recent_event(recent, event.event_id)
                if best:
                    if not camera_name:
                        camera_name = camera_map.get(camera_id) or self._extract_camera_name(best)
                    if not face_name:
                        face_name = self._extract_face_name(best)
                    if is_known_face is None:
                        is_known_face = self._extract_is_known_face(best, face_name)
                    if person_detected is None:
                        person_detected = self._extract_person_detected(best)
                    if speech_detected is None:
                        speech_detected = self._extract_speech_detected(best)
        enriched = replace(event, camera_id=camera_id, camera_name=camera_name, face_name=face_name, is_known_face=is_known_face, person_detected=person_detected, speech_detected=speech_detected)
        self._logger.info("Event enriched. trigger_type=%s camera_id=%s camera_name=%s face_name=%s known=%s person=%s speech=%s", enriched.trigger_type, enriched.camera_id or "", enriched.camera_name or "", enriched.face_name or "", enriched.is_known_face, enriched.person_detected, enriched.speech_detected)
        return enriched

    def _find_camera_id_by_name(self, camera_map: Dict[str, str], camera_name: str) -> Optional[str]:
        wanted = camera_name.casefold().strip()
        for camera_id, name in camera_map.items():
            if name.casefold().strip() == wanted:
                return camera_id
        return None

    def _pick_best_recent_event(self, events: List[Dict[str, Any]], preferred_event_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not events:
            return None
        if preferred_event_id:
            for event in events:
                event_id = self._to_str(event.get("id"))
                if event_id and event_id == preferred_event_id:
                    return event
        scored: List[tuple[int, Dict[str, Any]]] = []
        for event in events:
            score = 0
            face_name = self._extract_face_name(event)
            if face_name:
                score += 100
            known = self._extract_is_known_face(event, face_name)
            if known is True:
                score += 25
            if self._extract_person_detected(event) is True:
                score += 10
            if self._extract_speech_detected(event) is True:
                score += 5
            score += min(self._extract_event_timestamp_ms(event), 2_000_000_000_000) // 1000
            scored.append((score, event))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    def _extract_event_timestamp_ms(self, payload: Dict[str, Any]) -> int:
        try:
            return int(payload.get("start"))
        except Exception:
            return 0

    def _extract_camera_name(self, payload: Dict[str, Any]) -> Optional[str]:
        camera = payload.get("camera")
        if isinstance(camera, dict):
            for key in ("name", "cameraName"):
                text = self._to_str(camera.get(key))
                if text:
                    return text
        for key in ("cameraName", "camera_name", "deviceName", "device_name"):
            text = self._to_str(payload.get(key))
            if text:
                return text
        return None

    def _extract_face_name(self, payload: Dict[str, Any]) -> Optional[str]:
        metadata = payload.get("metadata")
        candidates: List[Any] = [payload.get("faceName"), payload.get("recognizedName"), payload.get("recognized_name"), payload.get("personName")]
        if isinstance(metadata, dict):
            candidates.extend([metadata.get("faceName"), metadata.get("recognizedName"), metadata.get("recognized_name"), metadata.get("personName")])
            faces = metadata.get("detectedFaces")
            if isinstance(faces, list):
                for item in faces:
                    if not isinstance(item, dict):
                        continue
                    for key in ("name", "faceName", "recognizedName"):
                        candidates.append(item.get(key))
        for value in candidates:
            text = self._to_str(value)
            if text:
                return text
        return None

    def _extract_is_known_face(self, payload: Dict[str, Any], face_name: Optional[str]) -> Optional[bool]:
        for obj in (payload, payload.get("metadata")):
            if not isinstance(obj, dict):
                continue
            for key in ("isKnownFace", "knownFace", "known_face"):
                if key in obj:
                    return self._to_bool_or_none(obj.get(key))
        if face_name:
            return True
        return None

    def _extract_person_detected(self, payload: Dict[str, Any]) -> Optional[bool]:
        for obj in (payload, payload.get("metadata")):
            if not isinstance(obj, dict):
                continue
            for key in ("personDetected", "person_detected"):
                if key in obj:
                    return self._to_bool_or_none(obj.get(key))
        smart_types = self._extract_smart_detect_types(payload)
        if "person" in {x.casefold() for x in smart_types}:
            return True
        return None

    def _extract_speech_detected(self, payload: Dict[str, Any]) -> Optional[bool]:
        for obj in (payload, payload.get("metadata")):
            if not isinstance(obj, dict):
                continue
            for key in ("speechDetected", "speech_detected", "audioDetected", "audio_detected"):
                if key in obj:
                    return self._to_bool_or_none(obj.get(key))
        smart_types = self._extract_smart_detect_types(payload)
        lowered = {x.casefold() for x in smart_types}
        if "speech" in lowered or "audio" in lowered:
            return True
        return None

    def _extract_smart_detect_types(self, payload: Dict[str, Any]) -> List[str]:
        result: List[str] = []
        def add_value(value: Any) -> None:
            if not isinstance(value, str):
                return
            text = value.strip()
            if not text:
                return
            if text.casefold() not in {x.casefold() for x in result}:
                result.append(text)
        direct = payload.get("smartDetectTypes")
        if isinstance(direct, list):
            for item in direct:
                add_value(item)
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            nested = metadata.get("smartDetectTypes")
            if isinstance(nested, list):
                for item in nested:
                    add_value(item)
        return result

    def _to_str(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _to_bool_or_none(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
        return None
