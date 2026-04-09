from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from authorization import AuthorizationService
from diagnostics import DiagnosticsService
from event_resolver import EventResolver
from ha_client import HomeAssistantClient
from log_store import LogStore
from models import WebhookEvent
from protect_client import ProtectClient
from session_manager import SessionManager, SessionManagerError
from settings import AppSettings, SettingsError, load_settings
from stt import SpeechToTextService
from test_mode import TestModeRecorder
from voice_pipeline import VoicePipeline
from wakeword import WakeWordService
from webhook_server import WebhookServer


TRACE_LEVEL_NUM = 5


def _install_trace_level() -> None:
    if hasattr(logging, "TRACE"):
        return
    logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
    def trace(self: logging.Logger, message: str, *args, **kwargs) -> None:
        if self.isEnabledFor(TRACE_LEVEL_NUM):
            self._log(TRACE_LEVEL_NUM, message, args, **kwargs)
    logging.TRACE = TRACE_LEVEL_NUM  # type: ignore[attr-defined]
    logging.Logger.trace = trace  # type: ignore[attr-defined]


def _configure_logging(level_name: str) -> logging.Logger:
    _install_trace_level()
    level_map = {"trace": TRACE_LEVEL_NUM, "debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
    level = level_map.get(level_name.lower(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logger = logging.getLogger("unifi_voice_bridge")
    logger.setLevel(level)
    return logger


class AppRuntime:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.log_store = LogStore(settings, logger)
        self.protect_client = ProtectClient(settings, logger)
        self.event_resolver = EventResolver(self.protect_client, logger)
        self.authorization = AuthorizationService(settings, logger)
        self.ha_client = HomeAssistantClient(logger)
        self.session_manager = SessionManager(logger)
        self.stt_service = SpeechToTextService(settings, logger)
        self.wakeword_service = WakeWordService(settings, logger)
        self.voice_pipeline = VoicePipeline(settings, logger, protect_client=self.protect_client, session_manager=self.session_manager, stt_service=self.stt_service, wakeword_service=self.wakeword_service, ha_client=self.ha_client, log_store=self.log_store)
        self.test_mode = TestModeRecorder(settings, logger, protect_client=self.protect_client, session_manager=self.session_manager, log_store=self.log_store)
        self.diagnostics = DiagnosticsService(settings, logger, protect_client=self.protect_client)
        self.webhook_server = WebhookServer(settings, logger, self._handle_webhook, get_runtime_summary=self._get_runtime_summary, get_camera_diagnostics=self._get_camera_diagnostics, run_manual_test=self._run_manual_test)
        self._stop_event = asyncio.Event()
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.settings.addon.logs_dir.mkdir(parents=True, exist_ok=True)
        self.settings.addon.clips_dir.mkdir(parents=True, exist_ok=True)
        await self.protect_client.start()
        await self.ha_client.start()
        camera_map = await self.protect_client.get_camera_map(force=False)
        ha_ok = await self.ha_client.ping()
        rtsp_self_check = await self.diagnostics.get_rtsp_self_check_summary()
        self.log_store.write_startup_info({
            "message": "UniFi Voice Bridge booted successfully.",
            "enabled_cameras": [{"camera_id": c.camera_id, "camera_name": c.camera_name, "speaker_media_player": c.speaker_media_player, "tts_entity": c.tts_entity, "enabled": c.enabled, "rtsp_source_override": c.rtsp_source} for c in self.settings.cameras if c.enabled],
            "protect_camera_count": len(camera_map),
            "protect_camera_map": camera_map,
            "home_assistant_ok": ha_ok,
            "test_mode_enabled": self.settings.addon.test_mode_enabled,
            "test_mode_record_seconds": self.settings.addon.test_mode_record_seconds,
            "rtsp_self_check": rtsp_self_check,
        })
        self.log_store.write_health("starting", {"message": "Runtime initialized.", "home_assistant_ok": ha_ok, "test_mode_enabled": self.settings.addon.test_mode_enabled, "rtsp_self_check_success_count": rtsp_self_check["success_count"], "rtsp_self_check_failure_count": rtsp_self_check["failure_count"]})
        self._log_configuration_summary()
        self._log_rtsp_self_check(rtsp_self_check)
        await self.webhook_server.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat_loop")

    async def run(self) -> None:
        await self.start()
        self.logger.info("Application started and waiting for work.")
        await self._stop_event.wait()
        await self.stop()

    async def stop(self) -> None:
        self.logger.info("Stopping UniFi Voice Bridge...")
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await self.webhook_server.stop()
        await self.ha_client.close()
        await self.protect_client.close()
        self.log_store.write_health("stopped", {"message": "Application stopped cleanly."})

    def request_stop(self) -> None:
        self._stop_event.set()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                protect_ok = await self.protect_client.ping()
                ha_ok = await self.ha_client.ping()
                cleaned = await self.session_manager.cleanup_closed_sessions(keep_minutes=30)
                deleted_logs = self.log_store.cleanup_old_files(self.settings.addon.keep_log_days)
                self.log_store.write_health("running", {"camera_count": len(self.settings.cameras), "enabled_camera_count": sum(1 for c in self.settings.cameras if c.enabled), "protect_ok": protect_ok, "home_assistant_ok": ha_ok, "session_cleanup_count": cleaned, "deleted_log_files": deleted_logs, "test_mode_enabled": self.settings.addon.test_mode_enabled})
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("Heartbeat loop crashed: %s", exc)
            self.log_store.write_health("error", {"message": f"Heartbeat loop crashed: {exc}"})
            self.request_stop()

    async def _get_runtime_summary(self):
        return await self.diagnostics.get_runtime_summary()

    async def _get_camera_diagnostics(self):
        return await self.diagnostics.get_camera_diagnostics()

    async def _run_manual_test(self, camera_id: str):
        profile = self.diagnostics.get_camera_profile(camera_id)
        if profile is None:
            raise ValueError(f"Camera '{camera_id}' was not found in camera_profiles.yaml.")
        if not profile.enabled:
            raise ValueError(f"Camera '{camera_id}' is disabled.")
        return await self.test_mode.process_manual_test(profile)

    async def _handle_webhook(self, event: WebhookEvent):
        enriched = await self.event_resolver.enrich_event(event)
        result = await self.authorization.authorize_event(enriched)
        if not result.accepted:
            self.log_store.write_rejection(session_id=f"reject-{enriched.event_id or 'unknown'}", camera_id=result.camera_id, camera_name=result.camera_name, trigger_type=result.trigger_type, reason=result.reason or "rejected", face_name=result.face_name, is_known_face=result.is_known_face, person_detected=result.person_detected, speech_detected=result.speech_detected, event_id=result.event_id, metadata={"raw_payload": enriched.raw_payload})
            return result
        profile = self.settings.camera_map.get(result.camera_id)
        if profile is None:
            self.log_store.write_rejection(session_id=f"reject-{enriched.event_id or 'unknown'}", camera_id=result.camera_id, camera_name=result.camera_name, trigger_type=result.trigger_type, reason="camera_profile_missing_after_authorization", face_name=result.face_name, is_known_face=result.is_known_face, person_detected=result.person_detected, speech_detected=result.speech_detected, event_id=result.event_id, metadata={"raw_payload": enriched.raw_payload})
            return result
        try:
            if self.settings.addon.test_mode_enabled:
                await self.test_mode.process_trigger(result, profile)
            else:
                await self.voice_pipeline.process_trigger(result, profile)
            return result
        except SessionManagerError as exc:
            self.logger.warning("Could not start session: %s", exc)
            self.log_store.write_rejection(session_id=f"reject-{enriched.event_id or 'unknown'}", camera_id=result.camera_id, camera_name=result.camera_name, trigger_type=result.trigger_type, reason=str(exc), face_name=result.face_name, is_known_face=result.is_known_face, person_detected=result.person_detected, speech_detected=result.speech_detected, event_id=result.event_id, metadata={"raw_payload": enriched.raw_payload})
            return result

    def _log_configuration_summary(self) -> None:
        enabled = [c for c in self.settings.cameras if c.enabled]
        disabled = [c for c in self.settings.cameras if not c.enabled]
        self.logger.info("Loaded settings. protect_host=%s protect_port=%s cameras=%s enabled=%s disabled=%s test_mode=%s", self.settings.addon.protect_host, self.settings.addon.protect_port, len(self.settings.cameras), len(enabled), len(disabled), self.settings.addon.test_mode_enabled)
        for camera in enabled:
            self.logger.info("Enabled camera. id=%s name=%s speaker=%s tts=%s allowed_faces=%s rtsp_override=%s", camera.camera_id, camera.camera_name, camera.speaker_media_player or "", camera.tts_entity or "", ", ".join(camera.allowed_faces) if camera.allowed_faces else "(none)", camera.rtsp_source or "")
        for camera in disabled:
            self.logger.debug("Disabled camera. id=%s name=%s", camera.camera_id, camera.camera_name)

    def _log_rtsp_self_check(self, summary) -> None:
        checked = summary.get("checked_camera_count", 0)
        success = summary.get("success_count", 0)
        failure = summary.get("failure_count", 0)
        self.logger.info("Startup RTSP self-check complete. checked=%s success=%s failure=%s", checked, success, failure)
        for item in summary.get("successful_cameras", []):
            self.logger.info("RTSP OK. camera_id=%s camera_name=%s source=%s", item.get("camera_id") or "", item.get("camera_name") or "", item.get("resolved_rtsp_source") or "")
        for item in summary.get("failed_cameras", []):
            self.logger.warning("RTSP FAILED. camera_id=%s camera_name=%s source=%s error=%s", item.get("camera_id") or "", item.get("camera_name") or "", item.get("resolved_rtsp_source") or "", item.get("resolved_rtsp_error") or "unknown_error")


async def _async_main() -> int:
    try:
        preload_settings = load_settings()
    except SettingsError as exc:
        bootstrap_logger = _configure_logging("info")
        bootstrap_logger.error("Settings error: %s", exc)
        return 2
    except Exception as exc:
        bootstrap_logger = _configure_logging("info")
        bootstrap_logger.exception("Unhandled startup error while loading settings: %s", exc)
        return 3
    logger = _configure_logging(preload_settings.addon.log_level)
    runtime = AppRuntime(preload_settings, logger)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.request_stop)
        except NotImplementedError:
            pass
    try:
        await runtime.run()
        return 0
    except Exception as exc:
        logger.exception("Fatal runtime error: %s", exc)
        try:
            runtime.log_store.write_health("fatal_error", {"message": str(exc)})
        except Exception:
            pass
        return 4


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
