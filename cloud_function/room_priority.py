"""Stable room ordering from one per-run live-status snapshot, without IO."""


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
