from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


DEFAULT_OPTIONS_FILE = Path("/data/options.json")


@dataclass(frozen=True)
class AddonOptions:
    protect_host: str
    protect_port: int
    protect_allow_insecure_tls: bool
    protect_username: str
    protect_password: str
    webhook_bind_host: str
    webhook_bind_port: int
    webhook_shared_secret: str
    log_level: str
    keep_log_days: int
    default_language: str
    default_agent_id: Optional[str]
    config_file: Path
    logs_dir: Path
    clips_dir: Path
    save_audio_debug_default: bool
    rtsp_transport: str
    rtsp_prefer_secure: bool
    session_open_seconds: int
    session_extend_seconds: int
    person_hold_seconds: int
    cooldown_seconds: int
    test_mode_enabled: bool
    test_mode_record_seconds: int
    wakeword_enabled: bool
    wakeword_model: str
    wakeword_score_threshold_pct: int
    stt_engine: str
    stt_model: str
    stt_device: str
    stt_compute_type: str
    vad_enabled: bool
    command_max_ms: int
    silence_timeout_ms: int
    minimum_command_ms: int
    silence_rms_threshold: int


@dataclass(frozen=True)
class GlobalCameraDefaults:
    language: str
    agent_id: Optional[str]
    wake_word: str
    require_known_face: bool
    require_person_presence: bool
    response_enabled_default: bool


@dataclass(frozen=True)
class CameraProfile:
    camera_id: str
    camera_name: str
    enabled: bool
    rtsp_source: Optional[str]
    speaker_media_player: Optional[str]
    tts_entity: Optional[str]
    allowed_faces: List[str] = field(default_factory=list)
    session_open_seconds: int = 10
    cooldown_seconds: int = 20
    save_audio_debug: bool = False
    response_enabled: bool = True
    require_known_face: bool = True
    require_person_presence: bool = True
    person_sensor: Optional[str] = None
    language: str = "en"
    agent_id: Optional[str] = None
    wake_word: str = "hey_unifi"


@dataclass(frozen=True)
class AppSettings:
    addon: AddonOptions
    global_defaults: GlobalCameraDefaults
    cameras: List[CameraProfile]

    @property
    def camera_map(self) -> Dict[str, CameraProfile]:
        return {camera.camera_id: camera for camera in self.cameras}


class SettingsError(Exception):
    pass


def load_settings(options_file: Path = DEFAULT_OPTIONS_FILE) -> AppSettings:
    options_raw = _read_json_file(options_file)
    addon = _parse_addon_options(options_raw)

    profile_raw = _read_yaml_file(addon.config_file)
    global_defaults = _parse_global_defaults(profile_raw.get("global", {}), addon)
    cameras = _parse_camera_profiles(profile_raw.get("cameras"), addon, global_defaults)

    return AppSettings(addon=addon, global_defaults=global_defaults, cameras=cameras)


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SettingsError(f"Options file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SettingsError(f"Failed to parse JSON options file '{path}': {exc}") from exc


def _read_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SettingsError(f"Camera profile file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SettingsError(f"Failed to parse YAML file '{path}': {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SettingsError(f"Camera profile file '{path}' must contain a YAML object at the root.")
    return data


def _parse_addon_options(raw: Dict[str, Any]) -> AddonOptions:
    protect_host = _required_str(raw, "protect_host")
    protect_username = _required_str(raw, "protect_username")
    protect_password = _required_str(raw, "protect_password")
    protect_port = _int_in_range(raw.get("protect_port", 443), "protect_port", 1, 65535)
    webhook_bind_port = _int_in_range(raw.get("webhook_bind_port", 8099), "webhook_bind_port", 1, 65535)
    log_level = _normalize_log_level(raw.get("log_level", "info"))
    rtsp_transport = _enum_value(raw.get("rtsp_transport", "tcp"), "rtsp_transport", {"tcp", "udp"})
    stt_engine = _enum_value(raw.get("stt_engine", "external"), "stt_engine", {"faster_whisper", "whisper", "external"})
    return AddonOptions(
        protect_host=protect_host,
        protect_port=protect_port,
        protect_allow_insecure_tls=_to_bool(raw.get("protect_allow_insecure_tls", False)),
        protect_username=protect_username,
        protect_password=protect_password,
        webhook_bind_host=_non_empty_str(raw.get("webhook_bind_host", "0.0.0.0"), "webhook_bind_host"),
        webhook_bind_port=webhook_bind_port,
        webhook_shared_secret=_to_str(raw.get("webhook_shared_secret", "")).strip(),
        log_level=log_level,
        keep_log_days=_int_in_range(raw.get("keep_log_days", 14), "keep_log_days", 1, 365),
        default_language=_non_empty_str(raw.get("default_language", "en"), "default_language"),
        default_agent_id=_optional_str(raw.get("default_agent_id")),
        config_file=Path(_non_empty_str(raw.get("config_file", "/config/camera_profiles.yaml"), "config_file")),
        logs_dir=Path(_non_empty_str(raw.get("logs_dir", "/config/logs"), "logs_dir")),
        clips_dir=Path(_non_empty_str(raw.get("clips_dir", "/config/audio_clips"), "clips_dir")),
        save_audio_debug_default=_to_bool(raw.get("save_audio_debug_default", False)),
        rtsp_transport=rtsp_transport,
        rtsp_prefer_secure=_to_bool(raw.get("rtsp_prefer_secure", False)),
        session_open_seconds=_int_in_range(raw.get("session_open_seconds", 10), "session_open_seconds", 3, 60),
        session_extend_seconds=_int_in_range(raw.get("session_extend_seconds", 3), "session_extend_seconds", 1, 15),
        person_hold_seconds=_int_in_range(raw.get("person_hold_seconds", 2), "person_hold_seconds", 1, 10),
        cooldown_seconds=_int_in_range(raw.get("cooldown_seconds", 20), "cooldown_seconds", 0, 300),
        test_mode_enabled=_to_bool(raw.get("test_mode_enabled", True)),
        test_mode_record_seconds=_int_in_range(raw.get("test_mode_record_seconds", 5), "test_mode_record_seconds", 1, 30),
        wakeword_enabled=_to_bool(raw.get("wakeword_enabled", False)),
        wakeword_model=_non_empty_str(raw.get("wakeword_model", "/config/models/hey_unifi.tflite"), "wakeword_model"),
        wakeword_score_threshold_pct=_int_in_range(raw.get("wakeword_score_threshold_pct", 55), "wakeword_score_threshold_pct", 1, 100),
        stt_engine=stt_engine,
        stt_model=_non_empty_str(raw.get("stt_model", "small"), "stt_model"),
        stt_device=_non_empty_str(raw.get("stt_device", "auto"), "stt_device"),
        stt_compute_type=_non_empty_str(raw.get("stt_compute_type", "default"), "stt_compute_type"),
        vad_enabled=_to_bool(raw.get("vad_enabled", True)),
        command_max_ms=_int_in_range(raw.get("command_max_ms", 6000), "command_max_ms", 500, 15000),
        silence_timeout_ms=_int_in_range(raw.get("silence_timeout_ms", 1000), "silence_timeout_ms", 100, 5000),
        minimum_command_ms=_int_in_range(raw.get("minimum_command_ms", 350), "minimum_command_ms", 100, 5000),
        silence_rms_threshold=_int_in_range(raw.get("silence_rms_threshold", 450), "silence_rms_threshold", 1, 5000),
    )


def _parse_global_defaults(raw: Dict[str, Any], addon: AddonOptions) -> GlobalCameraDefaults:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError("The 'global' section in camera_profiles.yaml must be an object.")
    return GlobalCameraDefaults(
        language=_non_empty_str(raw.get("language", addon.default_language), "global.language"),
        agent_id=_optional_str(raw.get("agent_id", addon.default_agent_id)),
        wake_word=_non_empty_str(raw.get("wake_word", "hey_unifi"), "global.wake_word"),
        require_known_face=_to_bool(raw.get("require_known_face", True)),
        require_person_presence=_to_bool(raw.get("require_person_presence", True)),
        response_enabled_default=_to_bool(raw.get("response_enabled_default", True)),
    )


def _parse_camera_profiles(raw: Any, addon: AddonOptions, global_defaults: GlobalCameraDefaults) -> List[CameraProfile]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SettingsError("The 'cameras' section in camera_profiles.yaml must be a list.")
    cameras: List[CameraProfile] = []
    seen_camera_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SettingsError(f"Camera entry at index {index} must be an object.")
        prefix = f"cameras[{index}]"
        camera_id = _required_str(item, "camera_id", prefix=prefix)
        if camera_id in seen_camera_ids:
            raise SettingsError(f"Duplicate camera_id '{camera_id}' found in camera_profiles.yaml.")
        seen_camera_ids.add(camera_id)
        camera_name = _non_empty_str(item.get("camera_name", camera_id), f"{prefix}.camera_name")
        profile = CameraProfile(
            camera_id=camera_id,
            camera_name=camera_name,
            enabled=_to_bool(item.get("enabled", True)),
            rtsp_source=_optional_str(item.get("rtsp_source")),
            speaker_media_player=_optional_str(item.get("speaker_media_player")),
            tts_entity=_optional_str(item.get("tts_entity")),
            allowed_faces=_string_list(item.get("allowed_faces", []), f"{prefix}.allowed_faces"),
            session_open_seconds=_int_in_range(item.get("session_open_seconds", addon.session_open_seconds), f"{prefix}.session_open_seconds", 3, 60),
            cooldown_seconds=_int_in_range(item.get("cooldown_seconds", addon.cooldown_seconds), f"{prefix}.cooldown_seconds", 0, 300),
            save_audio_debug=_to_bool(item.get("save_audio_debug", addon.save_audio_debug_default)),
            response_enabled=_to_bool(item.get("response_enabled", global_defaults.response_enabled_default)),
            require_known_face=_to_bool(item.get("require_known_face", global_defaults.require_known_face)),
            require_person_presence=_to_bool(item.get("require_person_presence", global_defaults.require_person_presence)),
            person_sensor=_optional_str(item.get("person_sensor")),
            language=_non_empty_str(item.get("language", global_defaults.language), f"{prefix}.language"),
            agent_id=_optional_str(item.get("agent_id", global_defaults.agent_id)),
            wake_word=_non_empty_str(item.get("wake_word", global_defaults.wake_word), f"{prefix}.wake_word"),
        )
        cameras.append(profile)
    return cameras


def _required_str(raw: Dict[str, Any], key: str, prefix: Optional[str] = None) -> str:
    name = f"{prefix}.{key}" if prefix else key
    value = raw.get(key)
    value = _optional_str(value)
    if not value:
        raise SettingsError(f"Missing required setting: {name}")
    return value


def _non_empty_str(value: Any, name: str) -> str:
    result = _optional_str(value)
    if not result:
        raise SettingsError(f"Setting '{name}' cannot be empty.")
    return result


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    raise SettingsError(f"Cannot convert value '{value}' to boolean.")


def _int_in_range(value: Any, name: str, min_value: int, max_value: int) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise SettingsError(f"Setting '{name}' must be an integer.") from exc
    if result < min_value or result > max_value:
        raise SettingsError(f"Setting '{name}' must be between {min_value} and {max_value}.")
    return result


def _enum_value(value: Any, name: str, allowed: set[str]) -> str:
    text = _non_empty_str(value, name)
    if text not in allowed:
        raise SettingsError(f"Setting '{name}' must be one of: {', '.join(sorted(allowed))}")
    return text


def _normalize_log_level(value: Any) -> str:
    text = _non_empty_str(value, "log_level").lower()
    allowed = {"trace", "debug", "info", "warning", "error"}
    if text not in allowed:
        raise SettingsError(f"Setting 'log_level' must be one of: {', '.join(sorted(allowed))}")
    return text


def _string_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SettingsError(f"Setting '{name}' must be a list of strings.")
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
