"""stall_detector — streaming-json stall pattern detection (v2)."""

from collections import Counter

_REPEATED_ACTION_THRESHOLD = 4
_REPEATED_ERROR_THRESHOLD = 3
_MONOLOGUE_THRESHOLD = 3
_PING_PONG_CYCLES = 6
_FROZEN_HASH_COUNT = 5


def detect_stall_pattern(events: list[dict]) -> dict | None:
    """Detect OpenHands stall patterns from streaming-json event list."""
    if not events:
        return None

    # 1. Repeated action: same tool_use 4+ times in a row
    tool_uses = [(e["name"], str(e.get("input", {}))) for e in events if e.get("type") == "tool_use"]
    if len(tool_uses) >= _REPEATED_ACTION_THRESHOLD:
        for i in range(len(tool_uses) - _REPEATED_ACTION_THRESHOLD + 1):
            if all(tool_uses[i + j] == tool_uses[i] for j in range(_REPEATED_ACTION_THRESHOLD)):
                return {"pattern": "repeated_action"}

    # 2. Repeated error: same error 3+ times
    errors = [e.get("error", "") for e in events if e.get("type") == "tool_result" and e.get("error")]
    if len(errors) >= _REPEATED_ERROR_THRESHOLD:
        error_counts = Counter(errors)
        most_common, count = error_counts.most_common(1)[0]
        if count >= _REPEATED_ERROR_THRESHOLD:
            return {"pattern": "repeated_error"}

    # 3. Ping-pong: alternating between 2 actions 6+ cycles
    if len(tool_uses) >= _PING_PONG_CYCLES * 2:
        for i in range(len(tool_uses) - _PING_PONG_CYCLES * 2 + 1):
            slice_ = tool_uses[i:i + _PING_PONG_CYCLES * 2]
            if all(
                slice_[j * 2] == slice_[0]
                and slice_[j * 2 + 1] == slice_[1]
                and slice_[j * 2] != slice_[j * 2 + 1]
                for j in range(_PING_PONG_CYCLES)
            ):
                return {"pattern": "ping_pong"}

    # 4. Monologue: 3+ text events without intervening tool_use
    monologue_run = 0
    for e in events:
        if e.get("type") == "text":
            monologue_run += 1
            if monologue_run >= _MONOLOGUE_THRESHOLD:
                return {"pattern": "monologue"}
        elif e.get("type") == "tool_use":
            monologue_run = 0

    return None


class StallDetector:
    """Graduated stall response: warn first, kill second. Resets on new pattern."""

    def __init__(self) -> None:
        self._stall_count = 0
        self._warnings_sent = 0
        self._diff_hashes: list[str] = []
        self._last_pattern: str | None = None

    def on_stall_detected(self, info: dict) -> str:
        """Called when a stall pattern is detected. Returns action: warn | kill."""
        pattern = info.get("pattern", "")
        # Different pattern resets the counter
        if pattern != self._last_pattern:
            self._stall_count = 0
            self._last_pattern = pattern
        if self._stall_count > 0:
            self._stall_count += 1
            return "kill"
        self._stall_count = 1
        self._warnings_sent += 1
        return "warn"

    def record_diff_hash(self, h: str) -> None:
        self._diff_hashes.append(h)
        if len(self._diff_hashes) > _FROZEN_HASH_COUNT:
            self._diff_hashes.pop(0)

    def evaluate(self) -> str | None:
        """Evaluate combined signals. Returns action or None."""
        if self._stall_count > 0:
            return "kill"
        return None
