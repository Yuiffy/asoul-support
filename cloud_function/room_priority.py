"""Stable room ordering from one per-run live-status snapshot, without IO."""

LIKE_POLICY_VERSION = 'adaptive_lowest100_v3'
LIKE_MIN_ROOMS = 100
LIKE_BASE_LIMIT = 5000
LIKE_PROBE_LIMIT = 6000
LIKE_PRIORITY_RESERVE = 330  # Ten 30-click rounds plus one possible relight.


def adaptive_like_plan(targets, levels, priority_room=25788785, minimum=LIKE_MIN_ROOMS):
    """Choose the level of the Nth medal across the roster, including all ties.

    Offline medals participate in the threshold; live status is an action gate,
    not a reason to widen the threshold to higher-level medals.
    """
    rooms = {room for room, _ in targets}
    candidates = sorted((levels[room], room) for room in rooms
                        if type(levels.get(room)) is int and 0 < levels[room] <= 120)
    threshold = candidates[min(minimum, len(candidates)) - 1][0] if candidates else None
    eligible = [(level, room) for level, room in candidates if level <= threshold]
    selected = ([priority_room] if priority_room in rooms else []) + [
        room for _, room in eligible if room != priority_room]
    return {'version': LIKE_POLICY_VERSION, 'rooms': selected, 'threshold': threshold,
            'levels': [[room, level] for level, room in eligible],
            'minimum_rooms': minimum, 'verified_rooms': len(candidates)}


def select_like_targets(targets, levels, priority_room=25788785, count=15):
    """Fixed daily selection; unknown levels never outrank verified low medals."""
    rooms = {room for room, _ in targets}
    candidates = [room for room in rooms if room != priority_room
                  and type(levels.get(room)) is int and 0 < levels[room] <= 120]
    candidates.sort(key=lambda room: (levels[room], room))
    return ([priority_room] if priority_room in rooms else []) + candidates[:count]


def prioritize(targets, statuses):
    def rank(target):
        room, uid = target
        if room == 25788785:
            return 0
        status = statuses.get(room)
        return 1 if status == 1 else (2 if status == 0 else 3)
    return sorted(targets, key=rank)
