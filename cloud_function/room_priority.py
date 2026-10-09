"""Stable room ordering from one per-run live-status snapshot, without IO."""


def prioritize(targets, statuses):
    def rank(target):
        room, uid = target
        if room == 25788785:
            return 0
        status = statuses.get(room)
        return 1 if status == 1 else (2 if status == 0 else 3)
    return sorted(targets, key=rank)
