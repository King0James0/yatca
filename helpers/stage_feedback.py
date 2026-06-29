"""Pure decision logic for YATCA's opt-in staged long-task feedback.

Kept dependency-free (no aiogram / A0 imports) so the staging order and the
liveness verdict are unit-testable on their own. handler.py wraps these with the
actual Telegram I/O and the elapsed-time loop.

Ported from the prod standalone telegram bot's 3-stage feedback, with stage 3
reworked into a liveness check (never kill a running task on duration).
"""

STAGE1_MESSAGE = "⚙️ Working on the task provided..."
STAGE2_MESSAGE = "⏳ Task given requires more time, please wait..."
STAGE3_MESSAGE = "⏳ Task is taking longer than usual, check back later..."
STAGE3_TIMEOUT_MESSAGE = "❌ Task timed out. Please try again."

DEFAULT_STAGE1_SECONDS = 30
DEFAULT_STAGE2_SECONDS = 300
DEFAULT_STAGE3_SECONDS = 1800


def resolve_thresholds(bot_cfg):
    """Read the three thresholds from a bot config, falling back to prod defaults.

    A non-positive or non-integer value falls back to its default so a fat-fingered
    0 / blank can't disable a stage or fire it instantly.
    """
    cfg = bot_cfg or {}

    def _secs(name, default):
        try:
            v = int(cfg.get(name, default))
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    return (
        _secs("stage1_seconds", DEFAULT_STAGE1_SECONDS),
        _secs("stage2_seconds", DEFAULT_STAGE2_SECONDS),
        _secs("stage3_seconds", DEFAULT_STAGE3_SECONDS),
    )


def due_stage(elapsed, t1, t2, t3, sent1, sent2):
    """Which stage message is due right now: 3, 2, 1, or None.

    Higher stages win so a slow daemon wake never sends a stale earlier stage
    (e.g. don't emit stage 1 once we're already past stage 2's threshold).
    """
    if elapsed >= t3:
        return 3
    if not sent2 and elapsed >= t2:
        return 2
    if not sent1 and elapsed >= t1:
        return 1
    return None


def stage3_timeout_due(is_running, stop_set):
    """After the stage-3 note + a grace window, report a timeout only if the turn
    is neither still running nor cleanly finished.

    ``stop_set`` is the turn-over Event the auto-reply path sets on a clean finish;
    if it's set the real answer was just delivered, so stay silent.
    """
    return (not is_running) and (not stop_set)
