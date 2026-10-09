"""Two-hour admission windows and a bounded invocation budget, without IO."""
import math

INTERVAL_SECONDS = 7200
MAX_RUN_SECONDS = 6600
CLOUD_TIMEOUT_SECONDS = 6900


def reservation_slots(started_at, lifetime_seconds):
    """Fence every window an invocation may occupy, including manual starts."""
    if not math.isfinite(started_at) or not math.isfinite(lifetime_seconds) or lifetime_seconds <= 0:
        raise ValueError('Invalid invocation lifetime')
    first = int(started_at // INTERVAL_SECONDS)
    last = math.ceil((started_at + lifetime_seconds) / INTERVAL_SECONDS) - 1
    return range(first, last + 1)
