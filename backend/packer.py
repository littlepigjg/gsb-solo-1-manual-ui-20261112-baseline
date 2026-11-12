"""Deterministic MaxRects bin packing for sprite atlas layout.

The planner only works with plain integers and never touches pixels, which
keeps every placement reproducible and easy to unit test. Each sprite is
described by its content size; padding is inflated into the packing size so
that neighbouring sprites never end up closer than ``padding`` pixels.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def fits(self, other_width, other_height):
        return other_width <= self.width and other_height <= self.height


@dataclass
class PackedSprite:
    key: object
    width: int
    height: int
    x: int
    y: int
    rotated: bool


def _split(rect, placed):
    """Return the free rectangles left after occupying ``placed``."""
    if (placed.x >= rect.x + rect.width or placed.x + placed.width <= rect.x
            or placed.y >= rect.y + rect.height or placed.y + placed.height <= rect.y):
        return [rect]
    pieces = []
    if placed.x > rect.x:
        pieces.append(Rect(rect.x, rect.y, placed.x - rect.x, rect.height))
    if placed.x + placed.width < rect.x + rect.width:
        pieces.append(Rect(placed.x + placed.width, rect.y, rect.x + rect.width - (placed.x + placed.width), rect.height))
    if placed.y > rect.y:
        pieces.append(Rect(rect.x, rect.y, rect.width, placed.y - rect.y))
    if placed.y + placed.height < rect.y + rect.height:
        pieces.append(Rect(rect.x, placed.y + placed.height, rect.width, rect.y + rect.height - (placed.y + placed.height)))
    return pieces


def _prune(candidates):
    """Drop rectangles contained in another free rectangle."""
    survivors = []
    for index, rect in enumerate(candidates):
        redundant = False
        for other_index, other in enumerate(candidates):
            if index == other_index:
                continue
            inside = (other.x <= rect.x and other.y <= rect.y
                      and other.x + other.width >= rect.x + rect.width
                      and other.y + other.height >= rect.y + rect.height)
            if inside:
                redundant = True
                break
        if not redundant:
            survivors.append(rect)
    return survivors


def _best_position(free_rects, width, height, allow_rotation):
    """Best Short Side Fit with a deterministic tie-breaker."""
    best = None
    options = [(False, width, height)]
    if allow_rotation and (height, width) != (width, height):
        options.append((True, height, width))
    for rotated, placed_width, placed_height in options:
        for rect in free_rects:
            if not rect.fits(placed_width, placed_height):
                continue
            leftover_w = rect.width - placed_width
            leftover_h = rect.height - placed_height
            score = (min(leftover_w, leftover_h), max(leftover_w, leftover_h),
                     int(rotated), rect.y, rect.x)
            if best is None or score < best[0]:
                best = (score, Rect(rect.x, rect.y, placed_width, placed_height), rotated)
    return best


def pack(sprites, canvas_width, canvas_height, *, allow_rotation=False, padding=0):
    """Pack ``(key, width, height)`` items into a fixed canvas.

    Returns ``(placements, unpacked_keys)``. Placement order matches the
    internal deterministic sorting, not necessarily the input order; callers
    map results back through ``key``. Nothing is partially returned when the
    canvas is too small: every item that cannot be placed is listed in
    ``unpacked_keys`` so the caller can abort the whole production.
    """
    ordered = sorted(
        sprites,
        key=lambda item: (-max(item[1], item[2]), -(item[1] * item[2]),
                          -item[2], -item[1], str(item[0])),
    )
    free_rects = [Rect(0, 0, canvas_width, canvas_height)]
    placements = []
    unpacked = []
    for key, width, height in ordered:
        packed_width = width + padding * 2
        packed_height = height + padding * 2
        choice = _best_position(free_rects, packed_width, packed_height, allow_rotation)
        if choice is None:
            unpacked.append(key)
            continue
        _, target, rotated = choice
        free_rects = [
            piece
            for free in free_rects
            for piece in _split(free, target)
        ]
        free_rects = _prune(free_rects)
        placements.append(PackedSprite(
            key=key,
            width=width,
            height=height,
            x=target.x + padding,
            y=target.y + padding,
            rotated=rotated,
        ))
    return placements, unpacked
