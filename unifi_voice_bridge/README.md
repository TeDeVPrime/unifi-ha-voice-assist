# UniFi Voice Bridge

This is a Home Assistant add-on skeleton that bridges selected UniFi Protect cameras into a voice pipeline.

## Current baseline

This package is finalized for a safe first build with:
- Protect login/bootstrap
- per-camera config
- diagnostics endpoints
- RTSP self-check on startup
- webhook intake and authorization
- record-only test mode
- JSONL logging

Full voice mode is scaffolded too, but local wake word / STT dependencies are intentionally left optional.

## First recommended settings

Use these add-on settings first:

```yaml
wakeword_enabled: false
stt_engine: "external"
test_mode_enabled: true
test_mode_record_seconds: 5
rtsp_transport: "tcp"
rtsp_prefer_secure: false
```

## First recommended flow

1. Build/install the add-on
2. Start it
3. Check `/health`
4. Check `/diagnostics/summary`
5. Check `/diagnostics/cameras`
6. Trigger `/diagnostics/manual-test` for one enabled indoor camera

## Expected output files

- `/config/logs/startup.json`
- `/config/logs/health.json`
- `/config/logs/latest.json`
- `/config/logs/sessions-YYYY-MM-DD.jsonl`
- `/config/audio_clips/YYYY-MM-DD/*.wav`

## Example camera_profiles.yaml

See `camera_profiles.example.yaml`.
