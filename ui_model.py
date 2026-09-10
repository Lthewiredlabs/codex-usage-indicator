"""Display rules kept separate from GTK so missing and stale usage can be tested."""

from datetime import datetime
import math

STALE_AFTER_SECONDS = 150


def numeric(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def remaining(window, now):
    if not isinstance(window, dict) or not numeric(window.get("usedPercent")):
        return None
    reset = window.get("resetsAt")
    if numeric(reset) and reset <= now:
        return None  # A passed reset is not proof that the quota has replenished.
    return max(0, min(100, 100 - window["usedPercent"]))


def percent(value):
    if value is None:
        return "—"
    # Don't round a small, positive remainder down to a misleading zero.
    if 0 < value < 1:
        return "<1%"
    return f"{math.floor(value)}%"


def window_name(window, fallback):
    minutes = window.get("windowDurationMins") if isinstance(window, dict) else None
    if minutes == 10080:
        return "Weekly"
    if numeric(minutes) and minutes > 0:
        if minutes % 1440 == 0:
            return f"{minutes / 1440:g}-day"
        if minutes % 60 == 0:
            return f"{minutes / 60:g}-hour"
        return f"{minutes:g}-minute"
    return fallback


def reset_text(window, now):
    reset = window.get("resetsAt") if isinstance(window, dict) else None
    if not numeric(reset):
        return "Reset time unavailable"
    if reset <= now:
        return "Reset time passed; waiting for fresh usage"
    seconds = max(1, math.ceil(reset - now))
    minutes = math.ceil(seconds / 60)
    if minutes >= 1440:
        relative = f"{minutes // 1440}d {(minutes % 1440) // 60}h"
    elif minutes >= 60:
        relative = f"{minutes // 60}h {minutes % 60}m"
    else:
        relative = f"{minutes}m"
    try:
        local = datetime.fromtimestamp(reset).astimezone().strftime("%a %b %d, %I:%M %p %Z")
    except (OverflowError, OSError, ValueError):
        return "Reset time unavailable"
    return f"Resets in {relative} · {local}"


def build_view(snapshot, now, error=None, refreshing=False):
    buckets = snapshot.get("buckets", []) if isinstance(snapshot, dict) else []
    buckets = [bucket for bucket in buckets if isinstance(bucket, dict)]
    selected = next((b for b in buckets if b.get("id") == "codex"), buckets[0] if buckets else None)
    fetched = snapshot.get("fetchedAt") if isinstance(snapshot, dict) else None
    stale = bool(error) or not numeric(fetched) or now - fetched > STALE_AFTER_SECONDS
    if selected is None:
        label = "Codex …" if refreshing else "Codex —"
    else:
        primary = percent(remaining(selected.get("primary"), now))
        secondary = percent(remaining(selected.get("secondary"), now))
        prefix = "~" if stale else ""
        # Keep the named quota visible if an account only has a non-Codex bucket.
        name = "Codex" if selected.get("id") == "codex" else str(selected.get("name") or selected.get("id"))
        label = f"{name} {prefix}{primary} · {secondary}"

    rows = [("Codex usage · remaining", True)]
    if selected:
        first = window_name(selected.get("primary"), "First window")
        second = window_name(selected.get("secondary"), "Second window")
        rows.append((f"Panel order: {first.lower()} · {second.lower()}", False))
    if stale and buckets:
        rows.append(("~ Last known usage · refresh pending" if not error else "~ Last known usage · refresh failed", False))
    for bucket in buckets:
        rows.append((str(bucket.get("name") or bucket.get("id") or "Codex"), True))
        for key, fallback in (("primary", "First window"), ("secondary", "Second window")):
            window = bucket.get(key)
            name = window_name(window, fallback)
            value = percent(remaining(window, now))
            rows.append((f"{name}: {value} remaining" if value != "—" else f"{name}: unavailable", False))
            rows.append((reset_text(window, now), False))
    if error:
        rows.append((error, False))
    elif not buckets:
        rows.append(("Reading your Codex usage…" if refreshing else "Usage unavailable", False))
    if numeric(fetched):
        try:
            updated = datetime.fromtimestamp(fetched).astimezone().strftime("%I:%M:%S %p")
            rows.append((f"Last updated {updated}", False))
        except (OverflowError, OSError, ValueError):
            pass
    rows.append(("Refreshes every minute", False))
    return {"label": label, "rows": rows, "stale": stale}
