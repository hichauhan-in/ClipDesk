"""Split a transcript into windows the model can reason about.

A window is a run of consecutive segments whose combined text stays under a
character budget. Consecutive windows overlap by a few segments so a topic that
straddles a boundary is seen whole at least once.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipdesk.models import TranscriptSegment


@dataclass(slots=True)
class TranscriptWindow:
    index: int
    segments: list[TranscriptSegment]

    @property
    def start(self) -> float:
        return self.segments[0].start if self.segments else 0.0

    @property
    def end(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    @property
    def first_id(self) -> int:
        return self.segments[0].id if self.segments else 0

    @property
    def last_id(self) -> int:
        return self.segments[-1].id if self.segments else 0

    def render(self) -> str:
        """The transcript as the model sees it: one line per segment, with the
        segment id and start time so it can refer back to exact positions."""
        lines: list[str] = []
        for segment in self.segments:
            speaker = f"{segment.speaker}: " if segment.speaker else ""
            lines.append(
                f"[{segment.id}] {format_timestamp(segment.start)} "
                f"{speaker}{segment.text.strip()}"
            )
        return "\n".join(lines)


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_timestamp(value: str | float) -> float:
    """Accept ``83.5``, ``"83.5"``, ``"01:23"`` or ``"00:01:23.500"``.

    Models are inconsistent about which they return even when the prompt is
    explicit, so every time value coming back from the LLM goes through here.
    """
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    if not text:
        return 0.0
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return 0.0
        total = 0.0
        for number in numbers:
            total = total * 60 + number
        return max(0.0, total)
    try:
        return max(0.0, float(text))
    except ValueError:
        return 0.0


def build_windows(
    segments: list[TranscriptSegment],
    *,
    window_chars: int = 9000,
    overlap_chars: int = 600,
) -> list[TranscriptWindow]:
    if not segments:
        return []

    windows: list[TranscriptWindow] = []
    current: list[TranscriptSegment] = []
    current_chars = 0

    for segment in segments:
        cost = len(segment.text) + 24  # allow for the id/timestamp prefix
        if current and current_chars + cost > window_chars:
            windows.append(TranscriptWindow(len(windows), current))
            # Carry back enough trailing segments to cover the overlap budget.
            carried: list[TranscriptSegment] = []
            carried_chars = 0
            for previous in reversed(current):
                if carried_chars >= overlap_chars:
                    break
                carried.insert(0, previous)
                carried_chars += len(previous.text) + 24
            current = carried
            current_chars = carried_chars
        current.append(segment)
        current_chars += cost

    if current:
        windows.append(TranscriptWindow(len(windows), current))
    return windows
