"""smoke_stage_feedback — staged long-task feedback decision logic.

Covers threshold resolution (prod defaults + fat-finger fallback), the staging
order (higher stage wins, each fires once, no stale earlier stage), and the
stage-3 liveness verdict (alive => silent, clean-finish => silent, stuck => timeout).

Standalone: `python tests/smoke_stage_feedback.py` (no A0 / aiogram needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # plugin root
from helpers import stage_feedback as sf  # noqa: E402


def main():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # --- resolve_thresholds: prod defaults ---
    check(sf.resolve_thresholds({}) == (30, 300, 1800), "empty cfg != prod defaults")
    check(sf.resolve_thresholds(None) == (30, 300, 1800), "None cfg != prod defaults")
    check(
        sf.resolve_thresholds({"stage1_seconds": 10, "stage2_seconds": 60, "stage3_seconds": 600})
        == (10, 60, 600),
        "custom thresholds not honored",
    )
    # fat-finger / non-positive / junk all fall back to the default per-field
    check(
        sf.resolve_thresholds({"stage1_seconds": 0, "stage2_seconds": -5, "stage3_seconds": "x"})
        == (30, 300, 1800),
        "bad values did not fall back to defaults",
    )
    check(
        sf.resolve_thresholds({"stage1_seconds": "45"}) == (45, 300, 1800),
        "numeric string not parsed",
    )

    t1, t2, t3 = 30, 300, 1800

    # --- due_stage: nothing before the first threshold ---
    check(sf.due_stage(0, t1, t2, t3, False, False) is None, "stage fired at t=0")
    check(sf.due_stage(29, t1, t2, t3, False, False) is None, "stage fired before t1")

    # --- stage 1 ---
    check(sf.due_stage(30, t1, t2, t3, False, False) == 1, "stage1 not due at t1")
    check(sf.due_stage(100, t1, t2, t3, False, False) == 1, "stage1 not due mid-window")
    check(sf.due_stage(100, t1, t2, t3, True, False) is None, "stage1 re-fired after sent")

    # --- stage 2 (and: never send a stale stage 1 once past t2) ---
    check(sf.due_stage(300, t1, t2, t3, False, False) == 2, "stage2 not due at t2 (stale s1)")
    check(sf.due_stage(300, t1, t2, t3, True, False) == 2, "stage2 not due at t2")
    check(sf.due_stage(500, t1, t2, t3, True, True) is None, "stage2 re-fired after sent")

    # --- stage 3 wins unconditionally past t3 (even if 1/2 never sent) ---
    check(sf.due_stage(1800, t1, t2, t3, False, False) == 3, "stage3 not due at t3")
    check(sf.due_stage(9999, t1, t2, t3, True, True) == 3, "stage3 not due past t3")

    # --- stage3_timeout_due liveness verdict ---
    check(sf.stage3_timeout_due(is_running=True, stop_set=False) is False, "alive => should stay silent")
    check(sf.stage3_timeout_due(is_running=False, stop_set=True) is False, "clean finish => should stay silent")
    check(sf.stage3_timeout_due(is_running=False, stop_set=False) is True, "stuck/dead => should report timeout")
    check(sf.stage3_timeout_due(is_running=True, stop_set=True) is False, "running+done race => stay silent")

    # --- prod wordings preserved ---
    check(sf.STAGE1_MESSAGE.endswith("Working on the task provided..."), "stage1 wording changed")
    check("requires more time" in sf.STAGE2_MESSAGE, "stage2 wording changed")
    check("taking longer than usual" in sf.STAGE3_MESSAGE, "stage3 wording changed")
    check("timed out" in sf.STAGE3_TIMEOUT_MESSAGE.lower(), "timeout wording changed")

    total = 24
    if fails:
        print(f"FAIL {len(fails)}/{total}:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print(f"OK {total}/{total} stage-feedback checks passed")


if __name__ == "__main__":
    main()
