from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class IconCandidate:
    item: Any
    confidence: int
    box: tuple[int, int, int, int]
    method: str


def _groups(values: Iterable[int], gap: int = 2) -> list[int]:
    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return []
    groups: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [round(sum(group) / len(group)) for group in groups]


def _best_progression(points: list[int]) -> tuple[list[int], int] | None:
    """Find a repeated inventory-grid phase and its spacing."""
    best: tuple[list[int], int] | None = None
    for left_index, left in enumerate(points):
        for right in points[left_index + 1:]:
            spacing = right - left
            if not 40 <= spacing <= 128:
                continue
            phase = [left]
            expected = left + spacing
            while expected <= points[-1] + 2:
                match = next((point for point in points if abs(point - expected) <= 2), None)
                if match is not None:
                    phase.append(match)
                expected += spacing
            if len(phase) >= 4 and (best is None or len(phase) > len(best[0])):
                best = (phase, spacing)
    return best


def _inventory_region(image):
    """Return the dark inventory grid, its offset and the game's pixel scale."""
    import numpy as np

    # Hero Siege's inventory bevel uses this stable dark-red pixel. A tolerance
    # keeps detection working through ordinary PNG colour-profile conversion.
    target = np.array([18, 17, 25], dtype=np.int16)  # BGR
    delta = np.max(np.abs(image.astype(np.int16) - target), axis=2)
    grid = delta <= 6
    import cv2

    def projection_peaks(scores, threshold: int) -> list[int]:
        values = scores.astype(np.float32)
        local_maximum = cv2.dilate(
            values.reshape(1, -1), np.ones((1, 11), dtype=np.uint8)
        ).ravel()
        return _groups(
            np.flatnonzero((values >= threshold) & (values >= local_maximum))
        )

    columns = projection_peaks(grid.sum(axis=0), 20)
    rows = projection_peaks(grid.sum(axis=1), 18)
    x_progression = _best_progression(columns)
    y_progression = _best_progression(rows)
    if not x_progression or not y_progression:
        if image.shape[0] * image.shape[1] <= 1_000_000:
            return image, (0, 0), 2.0
        return None

    xs, x_spacing = x_progression
    ys, y_spacing = y_progression
    spacing = (x_spacing + y_spacing) / 2
    if abs(x_spacing - y_spacing) > max(4, spacing * 0.12):
        return None
    left = max(0, xs[0])
    right = min(image.shape[1], xs[-1] + 2)
    top = max(0, ys[0] - round(spacing))
    bottom = min(image.shape[0], ys[-1] + round(spacing))
    if right - left < spacing * 2 or bottom - top < spacing * 2:
        return None
    return image[top:bottom, left:right], (left, top), spacing / 32.0


def _iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    overlap = max(0, right - left) * max(0, bottom - top)
    if not overlap:
        return 0.0
    return overlap / float(aw * ah + bw * bh - overlap)


class ItemIconMatcher:
    """Conservative matcher for sprites drawn inside a Hero Siege inventory."""

    def __init__(self, atlas_path: Path, items: Iterable[Any]):
        self.atlas_path = atlas_path
        self.items = list(items)
        self._atlas = None
        self._prepared: dict[float, list[tuple[Any, Any, Any, Any]]] = {}
        self._sift_prepared: dict[float, list[tuple[Any, Any, Any, tuple[int, int]]]] = {}

    def _load_atlas(self):
        import cv2

        if self._atlas is None:
            self._atlas = cv2.imread(str(self.atlas_path), cv2.IMREAD_UNCHANGED)
            if self._atlas is None or self._atlas.ndim != 3 or self._atlas.shape[2] != 4:
                raise ValueError("атлас иконок Hero Siege повреждён")
        return self._atlas

    def _references(self, scale: float):
        import cv2
        import numpy as np

        key = round(scale * 4) / 4
        if key in self._prepared:
            return self._prepared[key]
        atlas = self._load_atlas()
        by_icon: dict[tuple[int, int, int, int], list[Any]] = {}
        for item in self.items:
            icon = getattr(item, "icon", None)
            if icon and len(icon) == 4:
                by_icon.setdefault(tuple(int(value) for value in icon), []).append(item)

        references = []
        for rect, owners in by_icon.items():
            # A shared picture cannot establish which of two differently named
            # items is present. It is safer to leave it for manual correction.
            names = {getattr(owner, "display_name", "") for owner in owners}
            if len(names) != 1:
                continue
            x, y, width, height = rect
            raw = atlas[y:y + height, x:x + width]
            if raw.size == 0 or raw.shape[:2] != (height, width):
                continue
            target_width = max(3, round(width * key))
            target_height = max(3, round(height * key))
            colour = cv2.resize(
                raw[:, :, :3], (target_width, target_height), interpolation=cv2.INTER_NEAREST
            )
            alpha = cv2.resize(
                raw[:, :, 3], (target_width, target_height), interpolation=cv2.INTER_NEAREST
            )
            mask = np.where(alpha > 32, 255, 0).astype(np.uint8)
            if np.count_nonzero(mask) < 24:
                continue
            references.append((owners[0], colour, mask, raw))
        self._prepared[key] = references
        return references

    @staticmethod
    def _peaks(scores, threshold: float, width: int, height: int):
        import cv2
        import numpy as np

        finite = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        local = cv2.dilate(finite, np.ones((5, 5), dtype=np.uint8))
        ys, xs = np.where((finite >= threshold) & (finite >= local - 1e-6))
        ordered = sorted(
            ((float(finite[y, x]), int(x), int(y)) for x, y in zip(xs, ys)),
            reverse=True,
        )
        kept: list[tuple[float, int, int]] = []
        radius_x, radius_y = max(5, width // 3), max(5, height // 3)
        for score, x, y in ordered:
            if any(abs(x - old_x) < radius_x and abs(y - old_y) < radius_y for _, old_x, old_y in kept):
                continue
            kept.append((score, x, y))
        return kept

    def _direct_matches(self, image, scale: float) -> list[IconCandidate]:
        import cv2
        import numpy as np

        found: list[IconCandidate] = []
        for item, colour, mask, _ in self._references(scale):
            height, width = colour.shape[:2]
            if width > image.shape[1] or height > image.shape[0]:
                continue
            try:
                scores = cv2.matchTemplate(
                    image, colour, cv2.TM_CCORR_NORMED, mask=mask
                )
            except cv2.error:
                continue
            for score, x, y in self._peaks(scores, 0.92, width, height):
                patch = image[y:y + height, x:x + width]
                active = mask > 0
                mae = float(
                    np.abs(patch.astype(np.int16) - colour.astype(np.int16))[active].mean()
                )
                # Exact PNG sprites score near 1. JPEG compression and the
                # selected-slot glow move them slightly, but random dark UI
                # shapes fail the colour-error half of this gate.
                if mae > 24 or (score < 0.95 and mae > 16):
                    continue
                confidence = round(min(100, score * 92 + max(0, 24 - mae) / 3))
                found.append(
                    IconCandidate(item, confidence, (x, y, width, height), "pixels")
                )
        return found

    def _sift_references(self, scale: float, sift):
        import cv2
        import numpy as np

        key = round(scale * 4) / 4
        if key in self._sift_prepared:
            return self._sift_prepared[key]
        prepared = []
        for item, colour, mask, _ in self._references(key):
            background = np.full_like(colour, (15, 6, 6))
            composite = np.where(mask[:, :, None] > 0, colour, background)
            gray = cv2.cvtColor(composite, cv2.COLOR_BGR2GRAY)
            points, descriptors = sift.detectAndCompute(gray, None)
            if descriptors is not None and len(descriptors) >= 3:
                prepared.append((item, points, descriptors, (colour.shape[1], colour.shape[0])))
        self._sift_prepared[key] = prepared
        return prepared

    def _feature_matches(self, image, scale: float) -> list[IconCandidate]:
        import cv2
        import numpy as np

        if not hasattr(cv2, "SIFT_create"):
            return []
        sift = cv2.SIFT_create(nfeatures=3000, contrastThreshold=0.01, edgeThreshold=5)
        screen_points, screen_descriptors = sift.detectAndCompute(
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), None
        )
        if screen_descriptors is None:
            return []
        matcher = cv2.BFMatcher(cv2.NORM_L2)
        found: list[IconCandidate] = []
        for item, points, descriptors, (width, height) in self._sift_references(scale, sift):
            pairs = matcher.knnMatch(descriptors, screen_descriptors, k=2)
            translations = []
            for first, second in pairs:
                if first.distance >= 0.8 * second.distance:
                    continue
                ref_x, ref_y = points[first.queryIdx].pt
                dst_x, dst_y = screen_points[first.trainIdx].pt
                translations.append((dst_x - ref_x, dst_y - ref_y, first))

            remaining = list(translations)
            while remaining:
                anchor_x, anchor_y, _ = remaining[0]
                cluster = [
                    entry for entry in remaining
                    if (entry[0] - anchor_x) ** 2 + (entry[1] - anchor_y) ** 2 <= 16
                ]
                cluster_pairs = {(entry[2].queryIdx, entry[2].trainIdx): entry for entry in cluster}
                cluster = list(cluster_pairs.values())
                used = {id(entry) for entry in cluster}
                remaining = [entry for entry in remaining if id(entry) not in used]
                if len(cluster) < 4:
                    continue
                distances = [entry[2].distance for entry in cluster]
                if sum(distances) / len(distances) > 140:
                    continue
                screen_xy = [screen_points[entry[2].trainIdx].pt for entry in cluster]
                spread = (
                    max(point[0] for point in screen_xy) - min(point[0] for point in screen_xy) + 1
                ) * (
                    max(point[1] for point in screen_xy) - min(point[1] for point in screen_xy) + 1
                )
                if spread < 40:
                    continue
                x = round(float(np.median([entry[0] for entry in cluster])))
                y = round(float(np.median([entry[1] for entry in cluster])))
                confidence = min(99, 82 + len(cluster) // 2)
                found.append(
                    IconCandidate(item, confidence, (x, y, width, height), "features")
                )
        return found

    @staticmethod
    def _deduplicate(candidates: list[IconCandidate], limit: int) -> list[IconCandidate]:
        kept: list[IconCandidate] = []
        for candidate in sorted(candidates, key=lambda value: value.confidence, reverse=True):
            duplicate = next(
                (
                    old for old in kept
                    if getattr(old.item, "item_id", None) == getattr(candidate.item, "item_id", None)
                    and _iou(old.box, candidate.box) >= 0.35
                ),
                None,
            )
            if duplicate:
                continue
            conflict = next((old for old in kept if _iou(old.box, candidate.box) >= 0.60), None)
            if conflict:
                continue
            kept.append(candidate)
            if len(kept) >= limit:
                break
        return sorted(kept, key=lambda value: (value.box[1], value.box[0]))

    def match(self, image_bytes: bytes, limit: int = 10) -> list[IconCandidate]:
        import cv2
        import numpy as np
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                rgb = np.array(source.convert("RGB"))
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("Discord-вложение не является читаемым изображением") from error
        image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        region = _inventory_region(image)
        if region is None:
            return []
        cropped, (offset_x, offset_y), scale = region
        scale = round(scale * 4) / 4
        if not 0.75 <= scale <= 4.0:
            return []
        candidates = self._direct_matches(cropped, scale)
        candidates.extend(self._feature_matches(cropped, scale))
        adjusted = [
            IconCandidate(
                candidate.item,
                candidate.confidence,
                (
                    candidate.box[0] + offset_x,
                    candidate.box[1] + offset_y,
                    candidate.box[2],
                    candidate.box[3],
                ),
                candidate.method,
            )
            for candidate in candidates
        ]
        return self._deduplicate(adjusted, limit)
