"""faster-whisper transcription, CPU-first.

The model is loaded from ``vendor/models/whisper`` so a machine that has been
bootstrapped once never touches the network again. `int8` on the CPU is the
right default for corporate hardware: no GPU requirement, modest RAM, and more
than accurate enough to reason about topics and timing.
"""

from __future__ import annotations

from pathlib import Path

from clipdesk.bootstrap.provision import prepare_whisper_env, whisper_cache_dir
from clipdesk.config import TranscriptionConfig
from clipdesk.models import Transcript, TranscriptSegment, TranscriptSource
from clipdesk.transcription.base import ProgressFn, TranscriptionError


class WhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, config: TranscriptionConfig, vendor_dir: Path) -> None:
        self.config = config
        self.vendor_dir = vendor_dir
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model

        prepare_whisper_env(self.vendor_dir)
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                "The speech-to-text engine is not installed. Re-run the launcher, or "
                'install it with: pip install -e ".[transcribe]" — or upload an '
                ".srt/.vtt transcript alongside the video to skip this step."
            ) from exc

        cache = str(whisper_cache_dir(self.vendor_dir))
        try:
            self._model = WhisperModel(
                self.config.model,
                device=self.config.device,
                compute_type=self.config.compute_type,
                download_root=cache,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Could not load the '{self.config.model}' speech-to-text model: {exc}"
            ) from exc
        return self._model

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        duration_s: float,
        on_progress: ProgressFn | None = None,
    ) -> Transcript:
        model = self._load()
        if on_progress:
            on_progress(0.0, f"Transcribing with '{self.config.model}'…")

        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=self.config.language,
                beam_size=self.config.beam_size,
                vad_filter=self.config.vad_filter,
                condition_on_previous_text=False,
            )
        except Exception as exc:
            raise TranscriptionError(f"Speech-to-text failed: {exc}") from exc

        total = duration_s or float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[TranscriptSegment] = []
        # faster-whisper yields lazily; consuming the generator is what does the
        # work, which is also what lets us report real progress.
        for raw in raw_segments:
            text = (raw.text or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    id=len(segments),
                    start=float(raw.start),
                    end=float(raw.end),
                    text=text,
                    no_speech_prob=getattr(raw, "no_speech_prob", None),
                )
            )
            if on_progress and total > 0:
                on_progress(
                    min(0.99, float(raw.end) / total),
                    f"Transcribing… {len(segments)} segments",
                )

        if on_progress:
            on_progress(1.0, f"Transcribed {len(segments)} segments")

        return Transcript(
            source=TranscriptSource.WHISPER,
            language=getattr(info, "language", None),
            model=self.config.model,
            duration_s=total,
            segments=segments,
        )
