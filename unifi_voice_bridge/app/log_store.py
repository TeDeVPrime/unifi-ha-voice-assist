from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from models import SessionLogRecord, VoiceSession
from settings import AppSettings


class LogStore:
    def __init__(self, settings: AppSettings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger.getChild("log_store")
        self._logs_dir = settings.addon.logs_dir
        self._clips_dir = settings.addon.clips_dir
        self._latest_file = self._logs_dir / "latest.json"
        self._startup_file = self._logs_dir / "startup.json"
        self._health_file = self._logs_dir / "health.json"
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._clips_dir.mkdir(parents=True, exist_ok=True)

    def write_startup_info(self, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "timestamp_utc": _utc_now().isoformat(),
            "status": "started",
            "protect_host": self._settings.addon.protect_host,
            "protect_port": self._settings.addon.protect_port,
            "webhook_bind_host": self._settings.addon.webhook_bind_host,
            "webhook_bind_port": self._settings.addon.webhook_bind_port,
            "config_file": str(self._settings.addon.config_file),
            "logs_dir": str(self._settings.addon.logs_dir),
            "clips_dir": str(self._settings.addon.clips_dir),
            "camera_count": len(self._settings.cameras),
            "enabled_camera_count": sum(1 for c in self._settings.cameras if c.enabled),
            "camera_ids": [c.camera_id for c in self._settings.cameras],
        }
        if extra:
            payload.update(extra)
        self._write_json(self._startup_file, payload)

    def write_health(self, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"timestamp_utc": _utc_now().isoformat(), "status": status}
        if extra:
            payload.update(extra)
        self._write_json(self._health_file, payload)

    def append_session(self, session: VoiceSession) -> Path:
        return self.append_record(SessionLogRecord.from_session(session))

    def append_record(self, record: SessionLogRecord) -> Path:
        day_file = self._daily_log_path(record.timestamp_utc)
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with day_file.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
        self._write_json(self._latest_file, record.to_dict())
        self._logger.debug("Session log written. session_id=%s camera_id=%s result=%s", record.session_id, record.camera_id, record.result)
        return day_file

    def write_rejection(self, session_id: str, camera_id: str, camera_name: str, trigger_type: str, reason: str, face_name: Optional[str] = None, is_known_face: Optional[bool] = None, person_detected: Optional[bool] = None, speech_detected: Optional[bool] = None, event_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Path:
        record = SessionLogRecord(
            timestamp_utc=_utc_now(),
            session_id=session_id,
            camera_id=camera_id,
            camera_name=camera_name,
            trigger_type=trigger_type,
            event_id=event_id,
            face_name=face_name,
            face_allowed=False,
            is_known_face=is_known_face,
            person_detected=person_detected,
            speech_detected=speech_detected,
            transcript=None,
            language=None,
            agent_id=None,
            assistant_response_type=None,
            assistant_response_speech=None,
            assistant_success_targets=[],
            assistant_failed_targets=[],
            speaker_media_player=None,
            tts_entity=None,
            state="rejected",
            result="rejected",
            rejection_reason=reason,
            audio_clip=None,
            metadata=metadata or {},
        )
        return self.append_record(record)

    def cleanup_old_files(self, keep_days: int = 14) -> int:
        if keep_days < 1:
            keep_days = 1
        deleted = 0
        cutoff = _utc_now() - timedelta(days=keep_days)
        for path in self._logs_dir.glob("sessions-*.jsonl"):
            try:
                dt = _extract_date_from_daily_log(path)
                if dt and dt < cutoff.date():
                    path.unlink(missing_ok=True)
                    deleted += 1
            except Exception as exc:
                self._logger.warning("Could not remove old log file '%s': %s", path, exc)
        return deleted

    def reserve_audio_clip_path(self, camera_id: str, session_id: str) -> Path:
        day_folder = self._clips_dir / _utc_now().strftime("%Y-%m-%d")
        day_folder.mkdir(parents=True, exist_ok=True)
        safe_camera_id = _safe_part(camera_id)
        safe_session_id = _safe_part(session_id)
        return day_folder / f"{safe_camera_id}-{safe_session_id}.wav"

    def _daily_log_path(self, timestamp_utc: datetime) -> Path:
        name = f"sessions-{timestamp_utc.astimezone(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        return self._logs_dir / name

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_part(value: str) -> str:
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_", "."}:
            allowed.append(ch)
        else:
            allowed.append("_")
    return "".join(allowed).strip("_") or "unknown"


def _extract_date_from_daily_log(path: Path):
    stem = path.stem
    prefix = "sessions-"
    if not stem.startswith(prefix):
        return None
    date_text = stem[len(prefix):]
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None
