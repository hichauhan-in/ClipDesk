"""First-run provisioning of ffmpeg and the speech-to-text model."""

from clipdesk.bootstrap.provision import (
    ComponentStatus,
    ProvisionError,
    component_statuses,
    ffmpeg_dir,
    ffmpeg_installed,
    prepare_whisper_env,
    provision,
    provision_all,
    whisper_cache_dir,
    whisper_installed,
    ytdlp_binary,
)

__all__ = [
    "ComponentStatus",
    "ProvisionError",
    "component_statuses",
    "ffmpeg_dir",
    "ffmpeg_installed",
    "prepare_whisper_env",
    "provision",
    "provision_all",
    "whisper_cache_dir",
    "whisper_installed",
    "ytdlp_binary",
]
