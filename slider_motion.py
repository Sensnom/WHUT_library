"""Generate a short, bounded slider gesture ending at the detected position."""

import math
import random
from dataclasses import dataclass


# A bounded live comparison favored this duration over the shorter schedule.
# Scale timestamps only; keep geometry, endpoint and retry intervals unchanged.
DRAG_TIME_SCALE = 1.5


@dataclass(frozen=True)
class MotionPoint:
    x: float
    y: float
    time_ms: float


def make_slider_motion(distance, maximum, *, rng=None):
    """Return relative cursor positions and absolute times after mouse-down."""
    if not math.isfinite(distance) or not math.isfinite(maximum) or not 0 < distance <= maximum:
        raise ValueError('Slider distance must lie inside the finite track bounds')
    rng = rng if rng is not None else random
    target = distance
    overshoot = min(rng.uniform(1.5, 4), maximum - target)
    peak = target + overshoot
    points = []
    elapsed = rng.uniform(30, 65)

    def segment(start, end, count, duration):
        nonlocal elapsed
        weights = [rng.uniform(.7, 1.3) for _ in range(count)]
        total = sum(weights)
        noise_x = noise_y = 0.0
        # MouseEvent.clientY is integer-valued in the deployed browser. A
        # subpixel-only arc can disappear entirely from the SDK's track.
        drift = rng.choice((-1, 1)) * rng.uniform(1.2, 1.8)
        for index, weight in enumerate(weights, 1):
            progress = index / count
            # Smooth acceleration and deceleration; noise fades at both endpoints.
            eased = progress ** 3 * (10 - 15 * progress + 6 * progress ** 2)
            envelope = math.sin(math.pi * progress) ** 2
            noise_x = .65 * noise_x + .35 * rng.uniform(-.35, .35)
            noise_y = .65 * noise_y + .35 * rng.uniform(-.4, .4)
            x = min(maximum, max(0, start + (end - start) * eased + noise_x * envelope))
            y = max(-2, min(2, drift * math.sin(math.pi * progress) + noise_y * envelope))
            elapsed += duration * weight / total
            points.append(MotionPoint(x, y, elapsed * DRAG_TIME_SCALE))

    segment(0, peak, rng.randint(26, 34), rng.uniform(650, 850))
    if overshoot > .1:
        segment(peak, target, rng.randint(6, 10), rng.uniform(170, 270))
    elapsed += rng.uniform(40, 85)
    points.append(MotionPoint(target, 0, elapsed * DRAG_TIME_SCALE))
    return points
