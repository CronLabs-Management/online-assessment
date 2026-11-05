from django import template

register = template.Library()


@register.filter
def format_hms(value):
    """
    Format duration in seconds into 'Xh Ym Zs' format.
    Examples:
        0 -> '0h 00m 00s'
        45 -> '0h 00m 45s'
        125 -> '0h 02m 05s'
        3665 -> '1h 01m 05s'
    """
    if value is None or value == "":
        return "—"
    try:
        total = int(round(float(value)))
    except (ValueError, TypeError):
        return str(value)

    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}h {m:02d}m {s:02d}s"


@register.filter
def format_hms_adaptive(value):
    """
    Adaptive duration format:
    - If hours > 0: 'Xh Ym Zs'
    - If minutes > 0: 'Xm Ys'
    - Otherwise: 'Xs'
    """
    if value is None or value == "":
        return "—"
    try:
        total = int(round(float(value)))
    except (ValueError, TypeError):
        return str(value)

    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"
