"""Recognize Bilibili's explicit unlit-medal task schema, without IO."""


def relight_task(data, kind):
    if data.get('is_lighted') is not False:
        return None
    tasks = data.get('task_info', [])
    if not isinstance(tasks, list) or len(tasks) != 2:
        return None
    expected = {'like': ('点赞30次', 30), 'sendDanmu': ('发弹幕10次', 10)}
    by_kind = {}
    for item in tasks:
        if not isinstance(item, dict):
            return None
        key = item.get('jump_type')
        if key not in expected or key in by_kind:
            return None
        if (item.get('sub_title') != '仅点亮' or item.get('title') != expected[key][0]
                or type(item.get('is_done')) is not bool):
            return None
        by_kind[key] = item
    item = by_kind.get(kind)
    return expected[kind][1] if item and not item['is_done'] else None
