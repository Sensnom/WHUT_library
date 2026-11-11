"""Normalize seats and exact timeline data from the official page."""

import re


def seat_order(seat):
    label = str(seat.get("label") or seat.get("name") or seat["id"])
    return [int(part) if part.isascii() and part.isdigit() else part.casefold()
            for part in re.split(r"([0-9]+)", label)]


def seat_row(seat):
    row = {"id": str(seat["id"]), "label": str(seat.get("label") or seat.get("name") or seat["id"]), "slots": None}
    if seat.get("lock") is True or seat.get("scene") is True:
        row["slots"] = []
        row["state"] = "locked" if seat.get("lock") is True else "onsite"
    return row


def time_chunks(start, end, step):
    """Use full grid cells and keep the leading and trailing partial cells."""
    while start < end:
        stop = min(end, (start // step + 1) * step)
        yield start, stop
        start = stop


def merge_free_chunk(slots, start, end):
    """Extend a consecutive run without crossing an unconfirmed time gap."""
    if slots and slots[-1][1] == start:
        slots[-1] = (slots[-1][0], end)
    else:
        slots.append((start, end))


def replace_free_window(slots, start, end, free):
    """Replace one newly checked window without discarding the rest of the day."""
    parts = []
    for a, b in slots:
        if a < start:
            parts.append((a, min(b, start)))
        if b > end:
            parts.append((max(a, end), b))
    if free:
        parts.append((start, end))
    merged = []
    for a, b in sorted(parts):
        if a >= b:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def seat_slots(seat, data, parse):
    row = seat_row(seat)
    if row.get("state"):
        return row

    if not isinstance(data, dict) or not isinstance(data.get("freeList"), list):
        return row
    intervals = set()
    for slot in data["freeList"]:
        try:
            start, end = parse(slot["label"].replace("–", "-").replace("—", "-"))
            if not 0 <= start < end <= 1440:
                return row
            intervals.add((start, end))
        except (AttributeError, KeyError, TypeError, ValueError):
            return row
    row["slots"] = sorted(intervals)
    return row
