"""Approval gate for anything that spends finite LiveKit Cloud quota.

LiveKit Inference (cloud STT/TTS/LLM) on the free Build plan has a hard monthly
cap (about $2.50 of credit, ~50 minutes, 5 concurrent STT streams). Once it is
used up, every request -- including the production support agent's -- fails
with HTTP 429 until the 1st of the next month. So benchmark scripts must not
use it without the owner's explicit OK.

Every script that calls LiveKit Cloud inference calls require_approval() first:

- interactive terminal: shows what would be used and asks; **no answer within
  30 seconds, or anything but "yes", skips the cloud tests**;
- no terminal (cron, CI, an agent running the script): skips, because nobody
  can answer -- unless --approved-livekit-usage was passed, which the operator
  (or an agent, only after the owner approved in chat) adds deliberately.
"""

from __future__ import annotations

import select
import sys

BUILD_PLAN_CREDIT_USD = 2.50
ASSUMED_STT_USD_PER_MIN = 0.01  # rough; see https://livekit.com/pricing/inference
PROMPT_TIMEOUT_S = 30


def estimate(audio_minutes: float) -> str:
    usd = audio_minutes * ASSUMED_STT_USD_PER_MIN
    pct = 100 * usd / BUILD_PLAN_CREDIT_USD
    return (f"~{audio_minutes:.1f} min of audio streamed to LiveKit Inference, roughly ${usd:.2f} "
            f"(~{pct:.0f}% of the Build plan's ${BUILD_PLAN_CREDIT_USD:.2f}/month credit at an assumed "
            f"${ASSUMED_STT_USD_PER_MIN}/min)")


def require_approval(what: str, audio_minutes: float, pre_approved: bool = False,
                     timeout_s: int = PROMPT_TIMEOUT_S) -> bool:
    """True only if the owner approves spending LiveKit Cloud quota."""
    print(f"\n*** {what} uses finite LiveKit Cloud quota: {estimate(audio_minutes)}.")
    print("*** When the quota runs out, the production support agent stops working until the 1st.")
    if pre_approved:
        print("*** Approved via --approved-livekit-usage.")
        return True
    if not sys.stdin.isatty():
        print("*** No terminal to ask for approval -> skipping the LiveKit Cloud tests. "
              "(Pass --approved-livekit-usage only after the owner has approved.)")
        return False
    print(f"*** Type 'yes' within {timeout_s} s to run them; anything else, or no answer, skips them: ",
          end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    answer = sys.stdin.readline().strip().lower() if ready else ""
    if answer == "yes":
        return True
    print("\n*** " + ("No answer" if not ready else "Not approved") + " -> skipping the LiveKit Cloud tests.")
    return False
