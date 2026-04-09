from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    LISTENING_FOR_WAKEWORD = "listening_for_wakeword"
    CAPTURING_COMMAND = "capturing_command"
    PROCESSING = "processing"
    RESPONDING = "responding"
    COOLDOWN = "cooldown"
    CLOSED = "closed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class WebhookEvent:
    raw_payload: Dict[str, Any]
    received_at_utc: datetime
    trigger_type: str
    camera_id: Optional[str] = None
    camera_name: Optional[str] = None
    event_id: Optional[str] = None
    face_name: Optional[str] = None
    is_known_face: Optional[bool] = None
    person_detected: Optional[bool] = None
    speech_detected: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["received_at_utc"] = self.received_at_utc.isoformat()
        return data


@dataclass(frozen=True)
class ResolvedTrigger:
    accepted: bool
    reason: Optional[str]
    trigger_type: str
    camera_id: str
    camera_name: str
    face_name: Optional[str]
    face_allowed: bool
    is_known_face: Optional[bool]
    person_detected: Optional[bool]
    speech_detected: Optional[bool]
    event_id: Optional[str]
    webhook_event: WebhookEvent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "trigger_type": self.trigger_type,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "face_name": self.face_name,
            "face_allowed": self.face_allowed,
            "is_known_face": self.is_known_face,
            "person_detected": self.person_detected,
            "speech_detected": self.speech_detected,
            "event_id": self.event_id,
            "webhook_event": self.webhook_event.to_dict(),
        }


@dataclass
class VoiceSession:
    session_id: str
    camera_id: str
    camera_name: str
    state: SessionState
    created_at_utc: datetime = field(default_factory=utc_now)
    opened_at_utc: Optional[datetime] = None
    last_activity_at_utc: Optional[datetime] = None
    closed_at_utc: Optional[datetime] = None
    trigger_type: str = "unknown"
    event_id: Optional[str] = None
    face_name: Optional[str] = None
    face_allowed: bool = False
    is_known_face: Optional[bool] = None
    person_detected: Optional[bool] = None
    speech_detected: Optional[bool] = None
    transcript: Optional[str] = None
    language: Optional[str] = None
    agent_id: Optional[str] = None
    assistant_response_type: Optional[str] = None
    assistant_response_speech: Optional[str] = None
    assistant_success_targets: List[str] = field(default_factory=list)
    assistant_failed_targets: List[str] = field(default_factory=list)
    speaker_media_player: Optional[str] = None
    tts_entity: Optional[str] = None
    result: Optional[str] = None
    rejection_reason: Optional[str] = None
    audio_clip: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_activity_at_utc = utc_now()

    def set_state(self, state: SessionState) -> None:
        self.state = state
        self.touch()
        if state in {SessionState.ARMED, SessionState.LISTENING_FOR_WAKEWORD, SessionState.CAPTURING_COMMAND, SessionState.PROCESSING, SessionState.RESPONDING, SessionState.COOLDOWN} and self.opened_at_utc is None:
            self.opened_at_utc = self.last_activity_at_utc
        if state in {SessionState.CLOSED, SessionState.REJECTED}:
            self.closed_at_utc = self.last_activity_at_utc

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "state": self.state.value,
            "created_at_utc": _dt_to_str(self.created_at_utc),
            "opened_at_utc": _dt_to_str(self.opened_at_utc),
            "last_activity_at_utc": _dt_to_str(self.last_activity_at_utc),
            "closed_at_utc": _dt_to_str(self.closed_at_utc),
            "trigger_type": self.trigger_type,
            "event_id": self.event_id,
            "face_name": self.face_name,
            "face_allowed": self.face_allowed,
            "is_known_face": self.is_known_face,
            "person_detected": self.person_detected,
            "speech_detected": self.speech_detected,
            "transcript": self.transcript,
            "language": self.language,
            "agent_id": self.agent_id,
            "assistant_response_type": self.assistant_response_type,
            "assistant_response_speech": self.assistant_response_speech,
            "assistant_success_targets": list(self.assistant_success_targets),
            "assistant_failed_targets": list(self.assistant_failed_targets),
            "speaker_media_player": self.speaker_media_player,
            "tts_entity": self.tts_entity,
            "result": self.result,
            "rejection_reason": self.rejection_reason,
            "audio_clip": self.audio_clip,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SessionLogRecord:
    timestamp_utc: datetime
    session_id: str
    camera_id: str
    camera_name: str
    trigger_type: str
    event_id: Optional[str]
    face_name: Optional[str]
    face_allowed: bool
    is_known_face: Optional[bool]
    person_detected: Optional[bool]
    speech_detected: Optional[bool]
    transcript: Optional[str]
    language: Optional[str]
    agent_id: Optional[str]
    assistant_response_type: Optional[str]
    assistant_response_speech: Optional[str]
    assistant_success_targets: List[str]
    assistant_failed_targets: List[str]
    speaker_media_player: Optional[str]
    tts_entity: Optional[str]
    state: str
    result: Optional[str]
    rejection_reason: Optional[str]
    audio_clip: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_session(cls, session: VoiceSession) -> "SessionLogRecord":
        ts = session.closed_at_utc or session.last_activity_at_utc or session.created_at_utc
        return cls(
            timestamp_utc=ts,
            session_id=session.session_id,
            camera_id=session.camera_id,
            camera_name=session.camera_name,
            trigger_type=session.trigger_type,
            event_id=session.event_id,
            face_name=session.face_name,
            face_allowed=session.face_allowed,
            is_known_face=session.is_known_face,
            person_detected=session.person_detected,
            speech_detected=session.speech_detected,
            transcript=session.transcript,
            language=session.language,
            agent_id=session.agent_id,
            assistant_response_type=session.assistant_response_type,
            assistant_response_speech=session.assistant_response_speech,
            assistant_success_targets=list(session.assistant_success_targets),
            assistant_failed_targets=list(session.assistant_failed_targets),
            speaker_media_player=session.speaker_media_player,
            tts_entity=session.tts_entity,
            state=session.state.value,
            result=session.result,
            rejection_reason=session.rejection_reason,
            audio_clip=session.audio_clip,
            metadata=dict(session.metadata),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "trigger_type": self.trigger_type,
            "event_id": self.event_id,
            "face_name": self.face_name,
            "face_allowed": self.face_allowed,
            "is_known_face": self.is_known_face,
            "person_detected": self.person_detected,
            "speech_detected": self.speech_detected,
            "transcript": self.transcript,
            "language": self.language,
            "agent_id": self.agent_id,
            "assistant_response_type": self.assistant_response_type,
            "assistant_response_speech": self.assistant_response_speech,
            "assistant_success_targets": list(self.assistant_success_targets),
            "assistant_failed_targets": list(self.assistant_failed_targets),
            "speaker_media_player": self.speaker_media_player,
            "tts_entity": self.tts_entity,
            "state": self.state,
            "result": self.result,
            "rejection_reason": self.rejection_reason,
            "audio_clip": self.audio_clip,
            "metadata": dict(self.metadata),
        }


def _dt_to_str(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
