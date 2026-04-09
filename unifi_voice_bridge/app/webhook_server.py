from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from aiohttp import web

from models import ResolvedTrigger, VoiceSession, WebhookEvent
from settings import AppSettings


ResolvedTriggerHandler = Callable[[WebhookEvent], Awaitable[ResolvedTrigger]]
DiagnosticsSummaryHandler = Callable[[], Awaitable[Dict[str, Any]]]
DiagnosticsCamerasHandler = Callable[[], Awaitable[list[Dict[str, Any]]]]
ManualTestHandler = Callable[[str], Awaitable[VoiceSession]]


class WebhookServer:
    def __init__(self, settings: AppSettings, logger: logging.Logger, on_trigger: ResolvedTriggerHandler, *, get_runtime_summary: Optional[DiagnosticsSummaryHandler] = None, get_camera_diagnostics: Optional[DiagnosticsCamerasHandler] = None, run_manual_test: Optional[ManualTestHandler] = None) -> None:
        self._settings = settings
        self._logger = logger.getChild("webhook")
        self._on_trigger = on_trigger
        self._get_runtime_summary = get_runtime_summary
        self._get_camera_diagnostics = get_camera_diagnostics
        self._run_manual_test = run_manual_test
        self._app = web.Application()
        self._app.add_routes([
            web.get("/health", self._handle_health),
            web.post("/webhook", self._handle_webhook),
            web.get("/diagnostics/summary", self._handle_diagnostics_summary),
            web.get("/diagnostics/cameras", self._handle_diagnostics_cameras),
            web.post("/diagnostics/manual-test", self._handle_manual_test),
        ])
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._settings.addon.webhook_bind_host, port=self._settings.addon.webhook_bind_port)
        await self._site.start()
        self._logger.info("Webhook server listening on %s:%s", self._settings.addon.webhook_bind_host, self._settings.addon.webhook_bind_port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "unifi_voice_bridge", "timestamp_utc": datetime.now(timezone.utc).isoformat()})

    async def _handle_diagnostics_summary(self, request: web.Request) -> web.Response:
        if not self._is_secret_valid(request):
            return web.json_response({"ok": False, "error": "invalid_secret"}, status=401)
        if self._get_runtime_summary is None:
            return web.json_response({"ok": False, "error": "diagnostics_not_available"}, status=404)
        try:
            data = await self._get_runtime_summary()
        except Exception as exc:
            self._logger.exception("Diagnostics summary failed: %s", exc)
            return web.json_response({"ok": False, "error": "diagnostics_failed", "message": str(exc)}, status=500)
        return web.json_response({"ok": True, "summary": data})

    async def _handle_diagnostics_cameras(self, request: web.Request) -> web.Response:
        if not self._is_secret_valid(request):
            return web.json_response({"ok": False, "error": "invalid_secret"}, status=401)
        if self._get_camera_diagnostics is None:
            return web.json_response({"ok": False, "error": "diagnostics_not_available"}, status=404)
        try:
            data = await self._get_camera_diagnostics()
        except Exception as exc:
            self._logger.exception("Diagnostics cameras failed: %s", exc)
            return web.json_response({"ok": False, "error": "diagnostics_failed", "message": str(exc)}, status=500)
        return web.json_response({"ok": True, "cameras": data})

    async def _handle_manual_test(self, request: web.Request) -> web.Response:
        if not self._is_secret_valid(request):
            return web.json_response({"ok": False, "error": "invalid_secret"}, status=401)
        if self._run_manual_test is None:
            return web.json_response({"ok": False, "error": "manual_test_not_available"}, status=404)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        camera_id = str(payload.get("camera_id") or request.query.get("camera_id") or "").strip()
        if not camera_id:
            return web.json_response({"ok": False, "error": "camera_id_required"}, status=400)
        try:
            session = await self._run_manual_test(camera_id)
        except Exception as exc:
            self._logger.exception("Manual test failed: %s", exc)
            return web.json_response({"ok": False, "error": "manual_test_failed", "message": str(exc)}, status=500)
        return web.json_response({"ok": True, "session": session.to_dict()})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        if not self._is_secret_valid(request):
            return web.json_response({"ok": False, "error": "invalid_secret"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            text = await request.text()
            self._logger.warning("Invalid webhook JSON body. body_preview=%s", text[:500])
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "payload_must_be_object"}, status=400)
        event = self._parse_webhook_event(payload)
        self._logger.info("Webhook received. trigger_type=%s camera_id=%s camera_name=%s face_name=%s", event.trigger_type, event.camera_id or "", event.camera_name or "", event.face_name or "")
        try:
            result = await self._on_trigger(event)
        except Exception as exc:
            self._logger.exception("Webhook handler failed: %s", exc)
            return web.json_response({"ok": False, "error": "handler_failed", "message": str(exc)}, status=500)
        return web.json_response({"ok": True, "accepted": result.accepted, "reason": result.reason, "camera_id": result.camera_id, "camera_name": result.camera_name, "face_name": result.face_name, "face_allowed": result.face_allowed, "is_known_face": result.is_known_face, "person_detected": result.person_detected, "speech_detected": result.speech_detected, "event_id": result.event_id, "trigger_type": result.trigger_type})

    def _is_secret_valid(self, request: web.Request) -> bool:
        expected = (self._settings.addon.webhook_shared_secret or "").strip()
        if not expected:
            return True
        provided = (request.headers.get("X-UniFi-Voice-Secret") or request.headers.get("X-Webhook-Secret") or request.query.get("secret") or "").strip()
        return provided == expected

    def _parse_webhook_event(self, payload: Dict[str, Any]) -> WebhookEvent:
        trigger_type = self._pick_string(payload, ["type", "eventType", "event_type", "alarmType"]) or "unknown"
        camera_id = self._extract_camera_id(payload)
        camera_name = self._extract_camera_name(payload)
        event_id = self._pick_string(payload, ["id", "eventId", "event_id", "alarmId"])
        face_name = self._extract_face_name(payload)
        smart_detect_types = self._extract_smart_detect_types(payload)
        person_detected = self._extract_person_detected(payload, smart_detect_types)
        speech_detected = self._extract_speech_detected(payload, smart_detect_types)
        is_known_face = self._extract_is_known_face(payload, face_name)
        return WebhookEvent(raw_payload=payload, received_at_utc=datetime.now(timezone.utc), trigger_type=trigger_type, camera_id=camera_id, camera_name=camera_name, event_id=event_id, face_name=face_name, is_known_face=is_known_face, person_detected=person_detected, speech_detected=speech_detected)

    def _extract_camera_id(self, payload: Dict[str, Any]) -> Optional[str]:
        camera = payload.get("camera")
        if isinstance(camera, str) and camera.strip():
            return camera.strip()
        if isinstance(camera, dict):
            for key in ("id", "_id", "cameraId"):
                value = self._to_str(camera.get(key))
                if value:
                    return value
        for key in ("cameraId", "camera_id", "deviceId", "device_id"):
            value = self._to_str(payload.get(key))
            if value:
                return value
        alarm = payload.get("alarm")
        if isinstance(alarm, dict):
            for key in ("cameraId", "deviceId"):
                value = self._to_str(alarm.get(key))
                if value:
                    return value
        return None

    def _extract_camera_name(self, payload: Dict[str, Any]) -> Optional[str]:
        camera = payload.get("camera")
        if isinstance(camera, dict):
            for key in ("name", "cameraName"):
                value = self._to_str(camera.get(key))
                if value:
                    return value
        for key in ("cameraName", "camera_name", "deviceName", "device_name"):
            value = self._to_str(payload.get(key))
            if value:
                return value
        return None

    def _extract_face_name(self, payload: Dict[str, Any]) -> Optional[str]:
        candidates = [self._pick_string(payload, ["faceName", "recognizedName", "recognized_name", "personName"])]
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            candidates.extend([self._pick_string(metadata, ["faceName", "recognizedName", "recognized_name", "personName"])])
            faces = metadata.get("detectedFaces")
            if isinstance(faces, list):
                for item in faces:
                    if not isinstance(item, dict):
                        continue
                    candidates.append(self._pick_string(item, ["faceName", "recognizedName", "name"]))
        for value in candidates:
            if value:
                return value
        return None

    def _extract_smart_detect_types(self, payload: Dict[str, Any]) -> list[str]:
        result: list[str] = []
        def add_value(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text and text.casefold() not in {x.casefold() for x in result}:
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

    def _extract_person_detected(self, payload: Dict[str, Any], smart_detect_types: list[str]) -> Optional[bool]:
        for key in ("personDetected", "person_detected"):
            if key in payload:
                return self._to_bool_or_none(payload.get(key))
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("personDetected", "person_detected"):
                if key in metadata:
                    return self._to_bool_or_none(metadata.get(key))
        lowered = {x.casefold() for x in smart_detect_types}
        if "person" in lowered:
            return True
        return None

    def _extract_speech_detected(self, payload: Dict[str, Any], smart_detect_types: list[str]) -> Optional[bool]:
        for key in ("speechDetected", "speech_detected", "audioDetected", "audio_detected"):
            if key in payload:
                return self._to_bool_or_none(payload.get(key))
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("speechDetected", "speech_detected", "audioDetected", "audio_detected"):
                if key in metadata:
                    return self._to_bool_or_none(metadata.get(key))
        lowered = {x.casefold() for x in smart_detect_types}
        if "speech" in lowered or "audio" in lowered:
            return True
        return None

    def _extract_is_known_face(self, payload: Dict[str, Any], face_name: Optional[str]) -> Optional[bool]:
        for key in ("isKnownFace", "knownFace", "known_face"):
            if key in payload:
                return self._to_bool_or_none(payload.get(key))
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("isKnownFace", "knownFace", "known_face"):
                if key in metadata:
                    return self._to_bool_or_none(metadata.get(key))
        if face_name:
            return True
        return None

    def _pick_string(self, payload: Dict[str, Any], keys: list[str]) -> Optional[str]:
        for key in keys:
            value = self._to_str(payload.get(key))
            if value:
                return value
        return None

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
