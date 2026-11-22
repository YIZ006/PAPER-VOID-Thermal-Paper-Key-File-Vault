"""PV-RIBBON v1 real-time OpenCV optical decoder, tile assembler, and webcam scanner.

Implements multi-pass resilient fiducial detection (adaptive thresholding with
3-level nested contour hierarchy, morphological repair, CLAHE + unsharp masking,
3-corner geometric prediction with local NCC template refinement, and multi-scale
radial bullseye template matching + NMS), perspective rectification to 512x384,
asymmetric beacon/center orientation resolution, and illumination-envelope inner-50%
core bit extraction with threshold-offset and sharpening fallbacks.

All image processing and tile reassembly operate strictly in RAM.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Final, Iterable, Optional

import cv2
import numpy as np

try:
    from . import codec
except ImportError:
    import codec


# ---------------------------------------------------------------------------
# Warped Canonical Geometry & Precomputed Cell Coordinates
# ---------------------------------------------------------------------------

WARP_CELL: Final[int] = 8
WARP_W: Final[int] = codec.GRID_COLS * WARP_CELL  # 512
WARP_H: Final[int] = codec.GRID_ROWS * WARP_CELL  # 384

_CORNER_ORDER: Final[tuple[str, ...]] = ("TL", "TR", "BR", "BL")

DST_FIDUCIAL_PTS: Final[np.ndarray] = np.array(
    [
        (
            codec.FIDUCIAL_CENTERS[name][0] * WARP_CELL - 0.5,
            codec.FIDUCIAL_CENTERS[name][1] * WARP_CELL - 0.5,
        )
        for name in _CORNER_ORDER
    ],
    dtype=np.float32,
)
# TL=(35.5, 35.5), TR=(475.5, 35.5), BR=(475.5, 347.5), BL=(35.5, 347.5)

DST_OUTER_CORNERS: Final[np.ndarray] = np.array(
    [
        (-0.5, -0.5),
        (WARP_W - 0.5, -0.5),
        (WARP_W - 0.5, WARP_H - 0.5),
        (-0.5, WARP_H - 0.5),
    ],
    dtype=np.float32,
)

ASPECT_55_39: Final[float] = 55.0 / 39.0
DIAG_CELLS: Final[float] = math.hypot(55.0, 39.0)  # 67.42403...

_DATA_ROWS: Final[np.ndarray] = np.array(
    [rc[0] for rc in codec.DATA_CELL_COORDS], dtype=np.intp
)
_DATA_COLS: Final[np.ndarray] = np.array(
    [rc[1] for rc in codec.DATA_CELL_COORDS], dtype=np.intp
)


def _coords_to_arrays(coords: Iterable[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray]:
    items = tuple(coords)
    return (
        np.array([rc[0] for rc in items], dtype=np.intp),
        np.array([rc[1] for rc in items], dtype=np.intp),
    )


_BEACON_ARRAYS: Final[dict[str, tuple[np.ndarray, np.ndarray]]] = {
    "TL": _coords_to_arrays(codec.TL_BEACON_COORDS),
    "TR": _coords_to_arrays(codec.TR_BEACON_COORDS),
    "BL": _coords_to_arrays(codec.BL_BEACON_COORDS),
    "BR": _coords_to_arrays(codec.BR_BEACON_COORDS),
}

# Quiet-zone white reference cells adjacent to each 7x7 fiducial
_QUIET_ARRAYS: Final[dict[str, tuple[np.ndarray, np.ndarray]]] = {
    "TL": _coords_to_arrays([(8, c) for c in range(1, 9)] + [(r, 8) for r in range(1, 8)]),
    "TR": _coords_to_arrays([(8, c) for c in range(55, 63)] + [(r, 55) for r in range(1, 8)]),
    "BL": _coords_to_arrays([(39, c) for c in range(1, 9)] + [(r, 8) for r in range(40, 47)]),
    "BR": _coords_to_arrays([(39, c) for c in range(55, 63)] + [(r, 55) for r in range(40, 47)]),
}

# Outer 7x7 ring black reference cells for each corner
def _build_ring_coords(top_r: int, left_c: int) -> list[tuple[int, int]]:
    coords: list[tuple[int, int]] = []
    for dr in range(7):
        for dc in range(7):
            if max(abs(dr - 3), abs(dc - 3)) == 3:
                coords.append((top_r + dr, left_c + dc))
    return coords


def _build_inner_ring_coords(center_r: int, center_c: int) -> list[tuple[int, int]]:
    return [
        (center_r + dr, center_c + dc)
        for dr in (-1, 0, 1)
        for dc in (-1, 0, 1)
        if not (dr == 0 and dc == 0)
    ]


def _build_mid_ring_coords(top_r: int, left_c: int) -> list[tuple[int, int]]:
    coords: list[tuple[int, int]] = []
    for dr in range(7):
        for dc in range(7):
            if max(abs(dr - 3), abs(dc - 3)) == 2:
                coords.append((top_r + dr, left_c + dc))
    return coords


_OUTER_RING_ARRAYS: Final[dict[str, tuple[np.ndarray, np.ndarray]]] = {
    "TL": _coords_to_arrays(_build_ring_coords(1, 1)),
    "TR": _coords_to_arrays(_build_ring_coords(1, 56)),
    "BL": _coords_to_arrays(_build_ring_coords(40, 1)),
    "BR": _coords_to_arrays(_build_ring_coords(40, 56)),
}

_MID_RING_ARRAYS: Final[dict[str, tuple[np.ndarray, np.ndarray]]] = {
    "TL": _coords_to_arrays(_build_mid_ring_coords(1, 1)),
    "TR": _coords_to_arrays(_build_mid_ring_coords(1, 56)),
    "BL": _coords_to_arrays(_build_mid_ring_coords(40, 1)),
    "BR": _coords_to_arrays(_build_mid_ring_coords(40, 56)),
}

_FIDUCIAL_CENTER_CELL: Final[dict[str, tuple[int, int]]] = {
    "TL": (4, 4),
    "TR": (4, 59),
    "BL": (43, 4),
    "BR": (43, 59),
}

_INNER_RING_ARRAYS: Final[dict[str, tuple[np.ndarray, np.ndarray]]] = {
    name: _coords_to_arrays(_build_inner_ring_coords(rc[0], rc[1]))
    for name, rc in _FIDUCIAL_CENTER_CELL.items()
}

_ENV_KERNEL: Final[np.ndarray] = np.ones((39, 39), dtype=np.uint8)
_MORPH_K3: Final[np.ndarray] = np.ones((3, 3), dtype=np.uint8)


def _build_canonical_bullseye_template() -> np.ndarray:
    """Build 72x72 uint8 synthetic bullseye with 1-cell white quiet border (WARP_CELL=8)."""
    tmpl = np.full((9 * WARP_CELL, 9 * WARP_CELL), 255, dtype=np.uint8)
    for dr in range(7):
        for dc in range(7):
            dist = max(abs(dr - 3), abs(dc - 3))
            if dist in (0, 1, 3):
                y0 = (dr + 1) * WARP_CELL
                x0 = (dc + 1) * WARP_CELL
                tmpl[y0 : y0 + WARP_CELL, x0 : x0 + WARP_CELL] = 0
    return cv2.GaussianBlur(tmpl, (3, 3), 0.7)


_CANONICAL_BULLSEYE_TEMPLATE: Final[np.ndarray] = _build_canonical_bullseye_template()


def _make_radial_bullseye_template(diam_px: int) -> np.ndarray:
    """Build rotation-invariant circular concentric bullseye template (7:5:3 ratio)."""
    d = max(13, int(diam_px))
    margin = max(3, int(round(d * 0.22)))
    size = (d + 2 * margin) | 1
    half = (size - 1) / 2.0
    yy, xx = np.ogrid[:size, :size]
    dist = np.sqrt((xx - half) ** 2 + (yy - half) ** 2)
    radius = d / 2.0

    tmpl = np.full((size, size), 255.0, dtype=np.float32)
    tmpl[(dist > (5.0 / 7.0) * radius) & (dist <= radius)] = 0.0
    tmpl[dist <= (3.0 / 7.0) * radius] = 0.0
    tmpl_u8 = np.clip(tmpl, 0, 255).astype(np.uint8)
    return cv2.GaussianBlur(tmpl_u8, (3, 3), 0.65)


_RADIAL_TEMPLATE_CACHE: dict[int, np.ndarray] = {}


def _get_radial_template(diam_px: int) -> np.ndarray:
    key = max(13, int(diam_px))
    tmpl = _RADIAL_TEMPLATE_CACHE.get(key)
    if tmpl is None:
        tmpl = _make_radial_bullseye_template(key)
        _RADIAL_TEMPLATE_CACHE[key] = tmpl
    return tmpl


# ---------------------------------------------------------------------------
# Public Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedTile:
    """Validated PV-RIBBON v1 tile decoded from an image frame."""

    file_id: int
    tile_index: int
    total_tiles: int
    payload: bytes
    rs_errors_corrected: int
    quad_pts: np.ndarray
    orientation_idx: int


@dataclass
class FrameScanResult:
    """Result of scanning a single BGR/grayscale image frame."""

    decoded_tiles: list[DecodedTile]
    candidate_quads: list[np.ndarray]
    annotated_bgr: np.ndarray


# ---------------------------------------------------------------------------
# Internal Candidate Structures
# ---------------------------------------------------------------------------


@dataclass
class _FiducialCandidate:
    center: tuple[float, float]
    size: float
    score: float
    source: str


@dataclass
class _QuadCandidate:
    pts: np.ndarray  # (4, 2) float32 clockwise around centroid
    score: float
    used_indices: tuple[int, ...]


# ---------------------------------------------------------------------------
# Image Conversion & Fiducial Detection Passes
# ---------------------------------------------------------------------------


def _ensure_bgr_and_gray(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("Input image must be a non-empty NumPy array")

    arr = image
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)

    if arr.ndim == 2:
        gray = arr
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    elif arr.ndim == 3 and arr.shape[2] == 3:
        bgr = arr
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        raise ValueError(f"Unsupported image shape {arr.shape}")
    return bgr, gray


def _contour_center(cnt: np.ndarray) -> tuple[float, float]:
    rect_center, _, _ = cv2.minAreaRect(cnt)
    return float(rect_center[0]), float(rect_center[1])


def _find_bullseye_contours(
    binary_inv: np.ndarray,
    source_tag: str,
) -> list[_FiducialCandidate]:
    """Detect 3-level nested concentric squares (7x7 -> 5x5 -> 3x3) via RETR_TREE."""
    contours, hierarchy = cv2.findContours(
        binary_inv, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None or len(contours) == 0:
        return []

    hier = hierarchy[0]
    results: list[_FiducialCandidate] = []

    for i0, c0 in enumerate(contours):
        child1 = int(hier[i0][2])
        if child1 < 0:
            continue

        area0 = cv2.contourArea(c0)
        if area0 < 140.0:
            continue

        rect0 = cv2.minAreaRect(c0)
        (cx0, cy0), (w0, h0), _ = rect0
        if w0 < 11.0 or h0 < 11.0:
            continue
        aspect0 = min(w0, h0) / max(w0, h0)
        if aspect0 < 0.45:
            continue
        fill0 = area0 / max(w0 * h0, 1.0)
        if fill0 < 0.50:
            continue
        max_dim0 = max(w0, h0)

        # Traverse all direct hole children of c0 (noise specks may create extra siblings)
        c1_idx = child1
        matched = False
        while c1_idx >= 0 and not matched:
            c1 = contours[c1_idx]
            area1 = cv2.contourArea(c1)
            ratio10 = area1 / area0
            if 0.16 <= ratio10 <= 0.84 and area1 >= 25.0:
                rect1 = cv2.minAreaRect(c1)
                (cx1, cy1), (w1, h1), _ = rect1
                if (
                    w1 >= 4.0
                    and h1 >= 4.0
                    and min(w1, h1) / max(w1, h1) >= 0.42
                    and math.hypot(cx1 - cx0, cy1 - cy0) <= 0.25 * max_dim0
                ):
                    c2_idx = int(hier[c1_idx][2])
                    while c2_idx >= 0:
                        c2 = contours[c2_idx]
                        area2 = cv2.contourArea(c2)
                        ratio21 = area2 / area1
                        if 0.06 <= ratio21 <= 0.85 and area2 >= 10.0:
                            rect2 = cv2.minAreaRect(c2)
                            (cx2, cy2), (w2, h2), _ = rect2
                            if (
                                w2 >= 2.0
                                and h2 >= 2.0
                                and min(w2, h2) / max(w2, h2) >= 0.38
                                and math.hypot(cx2 - cx0, cy2 - cy0) <= 0.25 * max_dim0
                            ):
                                center_x = 0.20 * cx0 + 0.40 * cx1 + 0.40 * cx2
                                center_y = 0.20 * cy0 + 0.40 * cy1 + 0.40 * cy2
                                size_est = 0.5 * (w0 + h0)
                                ratio_err = abs(ratio10 - 0.51) + abs(ratio21 - 0.36)
                                score = max(0.2, 1.5 - ratio_err) * min(1.0, aspect0 / 0.85)
                                _merge_candidates(
                                    results,
                                    (
                                        _FiducialCandidate(
                                            center=(float(center_x), float(center_y)),
                                            size=float(size_est),
                                            score=float(score),
                                            source=source_tag,
                                        ),
                                    ),
                                )
                                matched = True
                                break
                        c2_idx = int(hier[c2_idx][0])
            c1_idx = int(hier[c1_idx][0])

    return results


def _merge_candidates(
    pool: list[_FiducialCandidate],
    incoming: Iterable[_FiducialCandidate],
    dist_factor: float = 0.45,
) -> list[_FiducialCandidate]:
    for cand in incoming:
        cx, cy = cand.center
        merged = False
        for idx, existing in enumerate(pool):
            ex, ey = existing.center
            thresh = max(8.0, dist_factor * min(cand.size, existing.size))
            if math.hypot(cx - ex, cy - ey) <= thresh:
                if cand.score > existing.score:
                    pool[idx] = cand
                merged = True
                break
        if not merged:
            pool.append(cand)
    return pool


def _adaptive_block_size(gray: np.ndarray, base: int = 31) -> int:
    min_dim = min(gray.shape[:2])
    scaled = max(base, int(round(min_dim * 0.045)))
    if scaled % 2 == 0:
        scaled += 1
    return min(scaled, 61)


def _pass1_adaptive_candidates(gray: np.ndarray) -> tuple[list[_FiducialCandidate], list[np.ndarray]]:
    """Pass 1: Fast Adaptive Gaussian Thresholding + RETR_TREE."""
    block = _adaptive_block_size(gray, 31)
    bin_main = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 7
    )
    cands = _find_bullseye_contours(bin_main, "p1_adap")
    binaries = [bin_main]

    # Also try finer block size on lower-res or 5x5 tiles if few candidates found
    if len(cands) < 4:
        block_small = max(15, (block // 2) | 1)
        if block_small != block:
            bin_fine = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block_small, 6
            )
            binaries.append(bin_fine)
            _merge_candidates(cands, _find_bullseye_contours(bin_fine, "p1_fine"))
    return cands, binaries


def _pass2_morph_repair(
    binaries: list[np.ndarray], pool: list[_FiducialCandidate]
) -> None:
    """Pass 2: Morphological close/open repair of nicked or bridged bullseye rings."""
    for bin_img in binaries:
        closed = cv2.morphologyEx(bin_img, cv2.MORPH_CLOSE, _MORPH_K3)
        _merge_candidates(pool, _find_bullseye_contours(closed, "p2_close"))

        opened = cv2.morphologyEx(bin_img, cv2.MORPH_OPEN, _MORPH_K3)
        _merge_candidates(pool, _find_bullseye_contours(opened, "p2_open"))

        close_open = cv2.morphologyEx(closed, cv2.MORPH_OPEN, _MORPH_K3)
        _merge_candidates(pool, _find_bullseye_contours(close_open, "p2_close_open"))


def _pass3_clahe_unsharp(
    gray: np.ndarray, pool: list[_FiducialCandidate]
) -> np.ndarray:
    """Pass 3: CLAHE (clipLimit=2.5, tileGridSize=(8,8)) + unsharp mask + adaptive threshold."""
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    gray_clahe = clahe.apply(gray)
    blurred = cv2.GaussianBlur(gray_clahe, (0, 0), sigmaX=1.4)
    sharp = cv2.addWeighted(gray_clahe, 1.65, blurred, -0.65, 0)

    block = _adaptive_block_size(sharp, 29)
    bin_sharp = cv2.adaptiveThreshold(
        sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 8
    )
    _merge_candidates(pool, _find_bullseye_contours(bin_sharp, "p3_clahe"))

    bin_closed = cv2.morphologyEx(bin_sharp, cv2.MORPH_CLOSE, _MORPH_K3)
    _merge_candidates(pool, _find_bullseye_contours(bin_closed, "p3_clahe_close"))
    return gray_clahe


def _pass5_radial_template_candidates(
    gray_clahe: np.ndarray,
    pool: list[_FiducialCandidate],
) -> None:
    """Pass 5: Multi-scale rotation-invariant radial bullseye matchTemplate + NMS."""
    h, w = gray_clahe.shape
    min_dim = min(h, w)
    if min_dim < 40:
        return

    scales: list[int] = []
    contour_sizes = [
        c.size for c in pool if c.source != "p5_radial" and c.size >= 14.0
    ]
    if contour_sizes:
        med_sz = float(np.median(contour_sizes))
        for mult in (0.78, 0.88, 0.96, 1.04, 1.14, 1.25):
            s = int(round(med_sz * mult))
            if 15 <= s <= min(115, int(min_dim * 0.28)):
                scales.append(s)
    else:
        # Full geometric pyramid fallback when zero contour bullseyes survived
        s_cur = 18.0
        max_s = min(110.0, min_dim * 0.26)
        while s_cur <= max_s:
            scales.append(int(round(s_cur)))
            s_cur *= 1.18

    unique_scales = sorted(set(scales))
    if not unique_scales:
        return

    half_gray: Optional[np.ndarray] = None
    peaks: list[_FiducialCandidate] = []

    for diam in unique_scales:
        if diam >= 44 and min_dim >= 240:
            if half_gray is None:
                half_gray = cv2.resize(
                    gray_clahe, (w // 2, h // 2), interpolation=cv2.INTER_AREA
                )
            src = half_gray
            d_eff = max(13, diam // 2)
            scale_mult = 2.0
        else:
            src = gray_clahe
            d_eff = diam
            scale_mult = 1.0

        tmpl = _get_radial_template(d_eff)
        th, tw = tmpl.shape
        if th >= src.shape[0] or tw >= src.shape[1]:
            continue

        res = cv2.matchTemplate(src, tmpl, cv2.TM_CCOEFF_NORMED)
        dil = cv2.dilate(res, np.ones((7, 7), dtype=np.uint8))
        local_max = (res == dil) & (res >= 0.42)
        ys, xs = np.where(local_max)
        if len(xs) == 0:
            continue

        scores = res[ys, xs]
        order = np.argsort(scores)[::-1][:16]
        half_tw = (tw - 1) * 0.5
        half_th = (th - 1) * 0.5

        for idx in order:
            sc = float(scores[idx])
            cx = (float(xs[idx]) + half_tw) * scale_mult
            cy = (float(ys[idx]) + half_th) * scale_mult
            peaks.append(
                _FiducialCandidate(
                    center=(cx, cy),
                    size=float(diam),
                    score=sc,
                    source="p5_radial",
                )
            )

    # 2D NMS across scales
    peaks.sort(key=lambda p: p.score, reverse=True)
    kept_radial: list[_FiducialCandidate] = []
    for p in peaks:
        px, py = p.center
        too_close = False
        for k in kept_radial:
            kx, ky = k.center
            if math.hypot(px - kx, py - ky) <= max(14.0, 0.75 * min(p.size, k.size)):
                too_close = True
                break
        if not too_close:
            kept_radial.append(p)
            if len(kept_radial) >= 12:
                break

    _merge_candidates(pool, kept_radial, dist_factor=0.50)


# ---------------------------------------------------------------------------
# Quad Formation & Pass 4 Missing-Corner Prediction + NCC Refinement
# ---------------------------------------------------------------------------


def _order_quad_clockwise(pts: np.ndarray) -> Optional[np.ndarray]:
    """Order 4 (x, y) points clockwise around centroid and validate convexity."""
    arr = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    cx, cy = arr.mean(axis=0)
    angles = np.arctan2(arr[:, 1] - cy, arr[:, 0] - cx)
    ordered = arr[np.argsort(angles)]

    # Verify non-degenerate convex quad with consistent winding
    area = float(cv2.contourArea(ordered))
    if area < 900.0:
        return None
    if not cv2.isContourConvex(ordered):
        return None
    return ordered


def _validate_quad_geometry(
    ordered: np.ndarray,
    sizes: Optional[tuple[float, ...]] = None,
    strict_scale: bool = False,
) -> Optional[float]:
    """Check 55:39 aspect & scale consistency; return geometry score or None."""
    edges = [
        float(np.linalg.norm(ordered[(i + 1) % 4] - ordered[i])) for i in range(4)
    ]
    min_e, max_e = min(edges), max(edges)
    if min_e < 40.0 or min_e / max_e < 0.42:
        return None

    h_avg = 0.5 * (edges[0] + edges[2])
    v_avg = 0.5 * (edges[1] + edges[3])
    long_e = max(h_avg, v_avg)
    short_e = min(h_avg, v_avg)
    ratio = long_e / max(short_e, 1e-6)
    if not (1.02 <= ratio <= 2.25):
        return None

    d0 = float(np.linalg.norm(ordered[2] - ordered[0]))
    d1 = float(np.linalg.norm(ordered[3] - ordered[1]))
    if min(d0, d1) / max(d0, d1, 1e-6) < 0.58:
        return None

    expected_bullseye = (long_e / 55.0) * 7.0
    if sizes:
        lo_mul, hi_mul = (0.58, 1.58) if strict_scale else (0.45, 2.15)
        for sz in sizes:
            if sz > 0.0 and not (lo_mul * expected_bullseye <= sz <= hi_mul * expected_bullseye):
                return None

    ratio_err = abs(math.log(ratio / ASPECT_55_39))
    opp_err = abs(edges[0] - edges[2]) / max_e + abs(edges[1] - edges[3]) / max_e
    return float(max(0.0, 2.0 - 2.0 * ratio_err - opp_err))


def _predict_and_refine_4th_corner(
    gray: np.ndarray,
    p_a: np.ndarray,
    p_b: np.ndarray,
    p_c: np.ndarray,
) -> tuple[np.ndarray, Optional[np.ndarray], float]:
    """Pass 4: Predict D = A + C - B and refine via affine-rectified NCC matchTemplate."""
    d_pred = (p_a + p_c - p_b).astype(np.float32)

    len_ba = float(np.linalg.norm(p_a - p_b))
    len_bc = float(np.linalg.norm(p_c - p_b))
    if len_bc > len_ba:
        p_a, p_c = p_c, p_a

    # 2D cross product determines handedness in y-down image space
    vec_a = p_a - p_b
    vec_c = p_c - p_b
    cross = float(vec_a[0] * vec_c[1] - vec_a[1] * vec_c[0])

    if cross >= 0.0:
        # Canonical (B=TL, A=TR, C=BL) -> D near BR=(475.5, 347.5)
        dst_3 = np.array(
            [(35.5, 35.5), (475.5, 35.5), (35.5, 347.5)], dtype=np.float32
        )
        d_canon = np.array([475.5, 347.5], dtype=np.float32)
    else:
        # Canonical (B=TR, A=TL, C=BR) -> D near BL=(35.5, 347.5)
        dst_3 = np.array(
            [(475.5, 35.5), (35.5, 35.5), (475.5, 347.5)], dtype=np.float32
        )
        d_canon = np.array([35.5, 347.5], dtype=np.float32)

    src_3 = np.stack([p_b, p_a, p_c], axis=0).astype(np.float32)
    M = cv2.getAffineTransform(src_3, dst_3)

    # Extract 192x192 rectified search patch centered on d_canon
    patch_half = 96
    patch_size = 2 * patch_half
    shift_M = M.copy()
    shift_M[0, 2] -= float(d_canon[0] - patch_half)
    shift_M[1, 2] -= float(d_canon[1] - patch_half)

    patch = cv2.warpAffine(
        gray,
        shift_M,
        (patch_size, patch_size),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    res = cv2.matchTemplate(patch, _CANONICAL_BULLSEYE_TEMPLATE, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)

    if max_val < 0.18:
        return d_pred, None, float(max_val)

    th, tw = _CANONICAL_BULLSEYE_TEMPLATE.shape
    peak_patch_x = float(max_loc[0]) + (tw - 1) * 0.5
    peak_patch_y = float(max_loc[1]) + (th - 1) * 0.5
    d_canon_refined_x = peak_patch_x + float(d_canon[0] - patch_half)
    d_canon_refined_y = peak_patch_y + float(d_canon[1] - patch_half)

    M_inv = cv2.invertAffineTransform(M)
    dx = float(
        M_inv[0, 0] * d_canon_refined_x + M_inv[0, 1] * d_canon_refined_y + M_inv[0, 2]
    )
    dy = float(
        M_inv[1, 0] * d_canon_refined_x + M_inv[1, 1] * d_canon_refined_y + M_inv[1, 2]
    )
    h, w = gray.shape
    if not (-0.15 * w <= dx <= 1.15 * w and -0.15 * h <= dy <= 1.15 * h):
        return d_pred, None, float(max_val)

    return d_pred, np.array([dx, dy], dtype=np.float32), float(max_val)


def _shares_claimed_corners(
    quad_pts: np.ndarray, claimed_centers: list[tuple[float, float, float]]
) -> bool:
    if not claimed_centers:
        return False
    hits = 0
    for pt in quad_pts:
        px, py = float(pt[0]), float(pt[1])
        for cx, cy, rad in claimed_centers:
            if math.hypot(px - cx, py - cy) <= rad:
                hits += 1
                break
        if hits >= 2:
            return True
    return False


def _generate_candidate_quads(
    gray: np.ndarray,
    pool: list[_FiducialCandidate],
    allow_3corner_prediction: bool = True,
    max_quads: int = 24,
    claimed_centers: Optional[list[tuple[float, float, float]]] = None,
) -> list[_QuadCandidate]:
    """Build ranked list of 4-point and Pass-4 predicted quads from fiducial pool."""
    n = len(pool)
    if n < 3:
        return []

    sorted_idx = sorted(
        range(n),
        key=lambda i: pool[i].score + (1.2 if pool[i].source != "p5_radial" else 0.0),
        reverse=True,
    )[:32]
    pts = np.array([pool[i].center for i in sorted_idx], dtype=np.float32)
    sizes = [pool[i].size for i in sorted_idx]
    scores = [pool[i].score for i in sorted_idx]
    sources = [pool[i].source for i in sorted_idx]
    is_contour = [src != "p5_radial" for src in sources]
    m = len(sorted_idx)

    quads: list[_QuadCandidate] = []
    seen_quads: list[np.ndarray] = []

    def _is_duplicate_quad(ordered: np.ndarray) -> bool:
        for prev in seen_quads:
            for shift in range(4):
                rot = np.roll(ordered, -shift, axis=0)
                if float(np.max(np.linalg.norm(rot - prev, axis=1))) <= 4.5:
                    return True
        return False

    # Build valid right-angle triplets once (used for O(N^3) 4-pt lookup & Pass 4)
    triplets_cands: list[tuple[float, int, int, int, float]] = []
    for b in range(m):
        pb = pts[b]
        for a in range(m):
            if a == b:
                continue
            va = pts[a] - pb
            la = float(np.linalg.norm(va))
            if la < 40.0:
                continue
            for c in range(a + 1, m):
                if c == b:
                    continue
                vc = pts[c] - pb
                lc = float(np.linalg.norm(vc))
                if lc < 40.0:
                    continue
                cos_t = float(np.dot(va, vc) / (la * lc))
                if abs(cos_t) > 0.48:  # ~61..119 deg
                    continue
                long_l, short_l = max(la, lc), min(la, lc)
                ratio = long_l / short_l
                if not (1.05 <= ratio <= 2.05):
                    continue
                exp_b = (long_l / 55.0) * 7.0
                if any(
                    sz > 0.0 and not (0.45 * exp_b <= sz <= 2.15 * exp_b)
                    for sz in (sizes[a], sizes[b], sizes[c])
                ):
                    continue
                contour_bonus = 1.0 * (
                    int(is_contour[a]) + int(is_contour[b]) + int(is_contour[c])
                )
                tri_sc = (
                    1.5
                    - abs(math.log(ratio / ASPECT_55_39)) * 2.0
                    - abs(cos_t)
                    + 0.4 * (scores[a] + scores[b] + scores[c])
                    + contour_bonus
                )
                triplets_cands.append((tri_sc, a, b, c, short_l))

    # 1) Direct 4-point combinations
    if 4 <= m <= 10:
        for i in range(m):
            for j in range(i + 1, m):
                for k in range(j + 1, m):
                    for l in range(k + 1, m):
                        sub_pts = pts[[i, j, k, l]]
                        ordered = _order_quad_clockwise(sub_pts)
                        if ordered is None:
                            continue
                        if claimed_centers and _shares_claimed_corners(ordered, claimed_centers):
                            continue
                        strict = all(is_contour[idx] for idx in (i, j, k, l))
                        geom_sc = _validate_quad_geometry(
                            ordered,
                            (sizes[i], sizes[j], sizes[k], sizes[l]),
                            strict_scale=strict,
                        )
                        if geom_sc is None:
                            continue
                        c_bonus = 0.8 * sum(int(is_contour[idx]) for idx in (i, j, k, l))
                        cand_sc = 2.5 + geom_sc + c_bonus + 0.4 * (scores[i] + scores[j] + scores[k] + scores[l])
                        orig_ids = tuple(sorted((sorted_idx[i], sorted_idx[j], sorted_idx[k], sorted_idx[l])))
                        quads.append(_QuadCandidate(ordered, cand_sc, orig_ids))
    elif m > 10:
        seen_4sets: set[tuple[int, int, int, int]] = set()
        for _, a, b, c, short_l in triplets_cands:
            d_pred = pts[a] + pts[c] - pts[b]
            dists = np.linalg.norm(pts - d_pred[None, :], axis=1)
            tol = 0.28 * short_l
            for d in range(m):
                if d in (a, b, c) or dists[d] > tol:
                    continue
                key = tuple(sorted((a, b, c, d)))
                if key in seen_4sets:
                    continue
                seen_4sets.add(key)
                ordered = _order_quad_clockwise(pts[list(key)])
                if ordered is None:
                    continue
                if claimed_centers and _shares_claimed_corners(ordered, claimed_centers):
                    continue
                strict = all(is_contour[idx] for idx in key)
                geom_sc = _validate_quad_geometry(
                    ordered,
                    tuple(sizes[idx] for idx in key),
                    strict_scale=strict,
                )
                if geom_sc is None:
                    continue
                c_bonus = 0.8 * sum(int(is_contour[idx]) for idx in key)
                cand_sc = 2.5 + geom_sc + c_bonus + 0.4 * sum(scores[idx] for idx in key)
                orig_ids = tuple(sorted(sorted_idx[idx] for idx in key))
                quads.append(_QuadCandidate(ordered, cand_sc, orig_ids))

    quads.sort(key=lambda q: q.score, reverse=True)
    deduped: list[_QuadCandidate] = []
    four_pt_limit = (max_quads - 8) if (allow_3corner_prediction and max_quads > 10) else max_quads
    for q in quads:
        if not _is_duplicate_quad(q.pts):
            deduped.append(q)
            seen_quads.append(q.pts)
            if len(deduped) >= four_pt_limit:
                break

    # 2) Pass 4: 3-point right-angle prediction + NCC template refinement
    if allow_3corner_prediction and m >= 3:
        triplets_cands.sort(reverse=True)
        for tri_sc, a, b, c, _ in triplets_cands[:12]:
            d_pred, d_ref, ncc_val = _predict_and_refine_4th_corner(
                gray, pts[a], pts[b], pts[c]
            )
            cands_4th = (
                ((d_ref, 1.5 * max(0.0, ncc_val)),)
                if (d_ref is not None and ncc_val >= 0.35)
                else ((d_ref, 1.5 * max(0.0, ncc_val)), (d_pred, -1.5))
            )
            for d_pt, bonus in cands_4th:
                if d_pt is None:
                    continue
                ordered = _order_quad_clockwise(
                    np.stack([pts[a], pts[b], pts[c], d_pt], axis=0)
                )
                if ordered is None:
                    continue
                if claimed_centers and _shares_claimed_corners(ordered, claimed_centers):
                    continue
                strict = all(is_contour[idx] for idx in (a, b, c))
                geom_sc = _validate_quad_geometry(
                    ordered, (sizes[a], sizes[b], sizes[c]), strict_scale=strict
                )
                if geom_sc is None:
                    continue
                if _is_duplicate_quad(ordered):
                    continue
                orig_ids = tuple(sorted((sorted_idx[a], sorted_idx[b], sorted_idx[c])))
                deduped.append(
                    _QuadCandidate(ordered, tri_sc + geom_sc + bonus, orig_ids)
                )
                seen_quads.append(ordered)
                if len(deduped) >= max_quads:
                    break
            if len(deduped) >= max_quads:
                break

    deduped.sort(key=lambda q: q.score, reverse=True)
    return deduped[:max_quads]


# ---------------------------------------------------------------------------
# Orientation Scoring & Bit Extraction
# ---------------------------------------------------------------------------


def _cell_core_medians(img_384x512: np.ndarray) -> np.ndarray:
    """Vectorized median of inner 50% core [r*8+2:r*8+6, c*8+2:c*8+6] -> (48, 64)."""
    blocks = (
        img_384x512.reshape(codec.GRID_ROWS, WARP_CELL, codec.GRID_COLS, WARP_CELL)[
            :, 2:6, :, 2:6
        ]
        .transpose(0, 2, 1, 3)
        .reshape(codec.GRID_ROWS, codec.GRID_COLS, 16)
    )
    return np.median(blocks, axis=2)


def _score_warped_orientation(
    warped: np.ndarray, quad_rot: np.ndarray
) -> tuple[float, float]:
    """Score orientation (score, ring_contrast) using beacons, hollow TL, and 55:39 aspect."""
    cores = _cell_core_medians(warped)

    corner_darkness: dict[str, float] = {}
    hollow_score: dict[str, float] = {}
    ring_contrasts: list[float] = []

    for name in _CORNER_ORDER:
        qr, qc = _QUIET_ARRAYS[name]
        rr, rc = _OUTER_RING_ARRAYS[name]
        mr, mc = _MID_RING_ARRAYS[name]
        br, bc = _BEACON_ARRAYS[name]
        ir, ic = _INNER_RING_ARRAYS[name]
        cr, cc = _FIDUCIAL_CENTER_CELL[name]

        white_ref = float(np.median(cores[qr, qc]))
        mid_ref = float(np.median(cores[mr, mc]))
        black_ref = float(np.median(cores[rr, rc]))
        ring_contrasts.append(0.5 * (white_ref + mid_ref) - black_ref)

        beacon_med = float(np.median(cores[br, bc]))
        denom = max(white_ref - black_ref, 15.0)
        dark_norm = (white_ref - beacon_med) / denom
        corner_darkness[name] = max(-0.25, min(1.25, dark_norm))

        inner_ring_med = float(np.median(cores[ir, ic]))
        center_val = float(cores[cr, cc])
        hollow_norm = (center_val - inner_ring_med) / denom
        hollow_score[name] = max(-0.25, min(1.25, hollow_norm))

    other_dark = float(
        np.median([corner_darkness["TR"], corner_darkness["BL"], corner_darkness["BR"]])
    )
    beacon_contrast = corner_darkness["TL"] - other_dark

    other_hollow = float(
        np.median([hollow_score["TR"], hollow_score["BL"], hollow_score["BR"]])
    )
    hollow_contrast = hollow_score["TL"] - other_hollow

    top_len = float(np.linalg.norm(quad_rot[1] - quad_rot[0]))
    bot_len = float(np.linalg.norm(quad_rot[2] - quad_rot[3]))
    left_len = float(np.linalg.norm(quad_rot[3] - quad_rot[0]))
    right_len = float(np.linalg.norm(quad_rot[2] - quad_rot[1]))
    ratio = (top_len + bot_len) / max(left_len + right_len, 1e-6)
    aspect_score = -abs(math.log(max(ratio, 1e-3) / ASPECT_55_39))

    ring_contrasts.sort()
    # Second-highest corner contrast ensures >=2 corners are real bullseyes
    ring_score = ring_contrasts[2]
    ring_bonus = 2.0 * max(-1.0, min(1.0, ring_score / 50.0))
    total_score = float(
        3.0 * beacon_contrast + 1.0 * hollow_contrast + 1.5 * aspect_score + ring_bonus
    )
    return total_score, float(ring_score)


def _try_decode_warped(
    warped_gray: np.ndarray,
    deep: bool = True,
) -> Optional[codec.DecodedTileFrame]:
    """Extract bits using inner 50% core + local illumination envelope + fallback sweeps."""
    wm = cv2.medianBlur(warped_gray, 3)

    # Primary: local morphological envelope midpoint (39x39 smoothed)
    env_max = cv2.dilate(wm, _ENV_KERNEL)
    env_min = cv2.erode(wm, _ENV_KERNEL)
    midpoint = cv2.GaussianBlur(
        0.5 * (env_max.astype(np.float32) + env_min.astype(np.float32)),
        (15, 15),
        0,
    )

    cell_cores = _cell_core_medians(wm)[_DATA_ROWS, _DATA_COLS]
    mid_cores = _cell_core_medians(midpoint)[_DATA_ROWS, _DATA_COLS]
    diff = cell_cores - mid_cores

    fast_offsets = (0.0, -10.0, 10.0)
    full_offsets = (0.0, -10.0, 10.0, -20.0, 20.0, -30.0, 30.0)
    for offset in (full_offsets if deep else fast_offsets):
        bits = (diff < offset).astype(np.uint8)
        tile_bytes = np.packbits(bits, bitorder="big").tobytes()
        decoded = codec.decode_tile_bytes(tile_bytes)
        if decoded is not None:
            return decoded

    if not deep:
        return None

    # Fallback 1: Gaussian local mean threshold
    gmean = cv2.GaussianBlur(wm.astype(np.float32), (39, 39), 0)
    gmean_cores = _cell_core_medians(gmean)[_DATA_ROWS, _DATA_COLS]
    diff_g = cell_cores - gmean_cores
    for offset in (0.0, -12.0, 12.0, -24.0, 24.0):
        bits = (diff_g < offset).astype(np.uint8)
        tile_bytes = np.packbits(bits, bitorder="big").tobytes()
        decoded = codec.decode_tile_bytes(tile_bytes)
        if decoded is not None:
            return decoded

    # Fallback 2: Unsharp-masked warped grayscale + midpoint (rescues severe defocus blur)
    blur = cv2.GaussianBlur(warped_gray, (0, 0), sigmaX=1.35)
    sharp = cv2.addWeighted(warped_gray, 1.85, blur, -0.85, 0)
    env_max_s = cv2.dilate(sharp, _ENV_KERNEL)
    env_min_s = cv2.erode(sharp, _ENV_KERNEL)
    mid_s = cv2.GaussianBlur(
        0.5 * (env_max_s.astype(np.float32) + env_min_s.astype(np.float32)),
        (15, 15),
        0,
    )
    diff_s = (
        _cell_core_medians(sharp)[_DATA_ROWS, _DATA_COLS]
        - _cell_core_medians(mid_s)[_DATA_ROWS, _DATA_COLS]
    )
    for offset in (0.0, -12.0, 12.0):
        bits = (diff_s < offset).astype(np.uint8)
        tile_bytes = np.packbits(bits, bitorder="big").tobytes()
        decoded = codec.decode_tile_bytes(tile_bytes)
        if decoded is not None:
            return decoded

    # Fallback 3: Raw warped grayscale without median blur (preserves thin 5x5 cell cores)
    diff_raw = _cell_core_medians(warped_gray)[_DATA_ROWS, _DATA_COLS] - mid_cores
    for offset in (0.0, -12.0, 12.0):
        bits = (diff_raw < offset).astype(np.uint8)
        tile_bytes = np.packbits(bits, bitorder="big").tobytes()
        decoded = codec.decode_tile_bytes(tile_bytes)
        if decoded is not None:
            return decoded

    return None


def _refine_warped_corners(
    gray: np.ndarray, quad_rot: np.ndarray, warped: np.ndarray
) -> tuple[Optional[np.ndarray], list[float], Optional[np.ndarray]]:
    """Refine intact fiducial centers via template matching and return (src_pts, mvs, H_inv)."""
    H = cv2.getPerspectiveTransform(quad_rot, DST_FIDUCIAL_PTS)
    try:
        H_inv = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None, [0.0, 0.0, 0.0, 0.0], None

    th, tw = _CANONICAL_BULLSEYE_TEMPLATE.shape  # 72x72
    roi_half = 48
    refined_warp_pts: list[list[float]] = []
    corner_mvs: list[float] = []

    for cx, cy in DST_FIDUCIAL_PTS:
        x0 = max(0, int(round(cx)) - roi_half)
        y0 = max(0, int(round(cy)) - roi_half)
        x1 = min(WARP_W, int(round(cx)) + roi_half)
        y1 = min(WARP_H, int(round(cy)) + roi_half)
        roi = warped[y0:y1, x0:x1]
        if roi.shape[0] < th or roi.shape[1] < tw:
            refined_warp_pts.append([float(cx), float(cy)])
            corner_mvs.append(0.0)
            continue
        res = cv2.matchTemplate(roi, _CANONICAL_BULLSEYE_TEMPLATE, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        corner_mvs.append(float(max_val))
        if max_val >= 0.62:
            rx = float(x0 + max_loc[0]) + (tw - 1) * 0.5
            ry = float(y0 + max_loc[1]) + (th - 1) * 0.5
            if math.hypot(rx - cx, ry - cy) <= 10.0:
                refined_warp_pts.append([rx, ry])
                continue
        refined_warp_pts.append([float(cx), float(cy)])

    pts_arr = np.array(refined_warp_pts, dtype=np.float32).reshape(1, 4, 2)
    refined_src = cv2.perspectiveTransform(pts_arr, H_inv).reshape(4, 2)
    return refined_src.astype(np.float32), corner_mvs, H_inv


def _decode_quad(gray: np.ndarray, quad_pts: np.ndarray) -> Optional[DecodedTile]:
    """Evaluate cyclic orientations for a quad with fast ring pruning and decode."""
    rot_quads = [np.roll(quad_pts, -k, axis=0).astype(np.float32) for k in range(4)]

    # Warp rotations 0 and 1; rotations 2 and 3 are exact 180-deg mirrors
    M0 = cv2.getPerspectiveTransform(rot_quads[0], DST_FIDUCIAL_PTS)
    w0 = cv2.warpPerspective(gray, M0, (WARP_W, WARP_H), flags=cv2.INTER_LINEAR)
    M1 = cv2.getPerspectiveTransform(rot_quads[1], DST_FIDUCIAL_PTS)
    w1 = cv2.warpPerspective(gray, M1, (WARP_W, WARP_H), flags=cv2.INTER_LINEAR)

    warped_views = [
        w0,
        w1,
        cv2.rotate(w0, cv2.ROTATE_180),
        cv2.rotate(w1, cv2.ROTATE_180),
    ]

    scored: list[tuple[float, float, int]] = []
    for k in range(4):
        sc, rc = _score_warped_orientation(warped_views[k], rot_quads[k])
        scored.append((sc, rc, k))

    max_rc = max(item[1] for item in scored)
    if max_rc < 10.0:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    top_sc, _, top_k = scored[0]
    second_sc, _, second_k = scored[1]

    # Phase 1: Fast offset try (0, -10, +10) on top-scoring orientation(s)
    fast_cands = [top_k] if (top_sc - second_sc >= 1.2) else [top_k, second_k]
    for k in fast_cands:
        frame = _try_decode_warped(warped_views[k], deep=False)
        if frame is not None:
            return DecodedTile(
                file_id=frame.file_id,
                tile_index=frame.tile_index,
                total_tiles=frame.total_tiles,
                payload=frame.payload,
                rs_errors_corrected=frame.rs_errors_corrected,
                quad_pts=rot_quads[k],
                orientation_idx=k,
            )

    # Phase 2: Deep threshold/unsharp sweeps + corner refinement on plausible PV-RIBBON views
    if top_sc > 0.25 or max_rc >= 25.0:
        deep_cands = [top_k] if (top_sc - second_sc >= 1.0) else [top_k, second_k]
        for k in deep_cands:
            frame = _try_decode_warped(warped_views[k], deep=True)
            if frame is not None:
                return DecodedTile(
                    file_id=frame.file_id,
                    tile_index=frame.tile_index,
                    total_tiles=frame.total_tiles,
                    payload=frame.payload,
                    rs_errors_corrected=frame.rs_errors_corrected,
                    quad_pts=rot_quads[k],
                    orientation_idx=k,
                )

        for k in deep_cands:
            refined_quad, corner_mvs, h_inv = _refine_warped_corners(
                gray, rot_quads[k], warped_views[k]
            )
            if refined_quad is None:
                continue
            M_ref = cv2.getPerspectiveTransform(refined_quad, DST_FIDUCIAL_PTS)
            w_ref = cv2.warpPerspective(
                gray, M_ref, (WARP_W, WARP_H), flags=cv2.INTER_LINEAR
            )
            frame = _try_decode_warped(w_ref, deep=True)
            if frame is not None:
                return DecodedTile(
                    file_id=frame.file_id,
                    tile_index=frame.tile_index,
                    total_tiles=frame.total_tiles,
                    payload=frame.payload,
                    rs_errors_corrected=frame.rs_errors_corrected,
                    quad_pts=refined_quad,
                    orientation_idx=k,
                )

            # Perturb the weakest-confidence corner when blotch clipped its edge
            if top_sc > 1.2 and h_inv is not None:
                weak_idx = int(np.argmin(corner_mvs))
                if corner_mvs[weak_idx] < 0.78:
                    base_warp = cv2.perspectiveTransform(
                        refined_quad.reshape(1, 4, 2),
                        cv2.getPerspectiveTransform(rot_quads[k], DST_FIDUCIAL_PTS),
                    ).reshape(4, 2)
                    for dx, dy in (
                        (-4.5, 0.0), (4.5, 0.0), (0.0, -4.5), (0.0, 4.5),
                        (-4.5, -4.5), (4.5, -4.5), (-4.5, 4.5), (4.5, 4.5),
                    ):
                        pw = base_warp.copy()
                        pw[weak_idx, 0] += dx
                        pw[weak_idx, 1] += dy
                        p_src = cv2.perspectiveTransform(
                            pw.reshape(1, 4, 2), h_inv
                        ).reshape(4, 2).astype(np.float32)
                        M_p = cv2.getPerspectiveTransform(p_src, DST_FIDUCIAL_PTS)
                        w_p = cv2.warpPerspective(
                            gray, M_p, (WARP_W, WARP_H), flags=cv2.INTER_LINEAR
                        )
                        frame = _try_decode_warped(w_p, deep=False)
                        if frame is not None:
                            return DecodedTile(
                                file_id=frame.file_id,
                                tile_index=frame.tile_index,
                                total_tiles=frame.total_tiles,
                                payload=frame.payload,
                                rs_errors_corrected=frame.rs_errors_corrected,
                                quad_pts=p_src,
                                orientation_idx=k,
                            )

        # Last-resort fast try on remaining orientations if beacon was heavily occluded
        for _, _, k in scored[2:]:
            frame = _try_decode_warped(warped_views[k], deep=False)
            if frame is not None:
                return DecodedTile(
                    file_id=frame.file_id,
                    tile_index=frame.tile_index,
                    total_tiles=frame.total_tiles,
                    payload=frame.payload,
                    rs_errors_corrected=frame.rs_errors_corrected,
                    quad_pts=rot_quads[k],
                    orientation_idx=k,
                )

    return None


# ---------------------------------------------------------------------------
# Overlay Drawing & Main Frame Decode API
# ---------------------------------------------------------------------------


def _draw_tile_overlay(annotated_bgr: np.ndarray, tile: DecodedTile) -> None:
    """Draw green polygon around tile outer boundary, cyan corners, and tile label."""
    H = cv2.getPerspectiveTransform(tile.quad_pts.astype(np.float32), DST_FIDUCIAL_PTS)
    try:
        H_inv = np.linalg.inv(H)
        outer = cv2.perspectiveTransform(
            DST_OUTER_CORNERS.reshape(1, 4, 2), H_inv
        ).reshape(4, 2)
    except np.linalg.LinAlgError:
        outer = tile.quad_pts

    poly_pts = np.round(outer).astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(annotated_bgr, [poly_pts], isClosed=True, color=(0, 230, 0), thickness=2)

    for idx, corner in enumerate(outer):
        cx, cy = int(round(float(corner[0]))), int(round(float(corner[1])))
        cv2.circle(annotated_bgr, (cx, cy), 5, (255, 255, 0), -1)
        if idx == 0:
            cv2.circle(annotated_bgr, (cx, cy), 8, (0, 220, 255), 2)

    for idx, fpt in enumerate(tile.quad_pts):
        fx, fy = int(round(float(fpt[0]))), int(round(float(fpt[1])))
        cv2.circle(annotated_bgr, (fx, fy), 4, (255, 255, 0), -1)
        if idx == 0:
            cv2.circle(annotated_bgr, (fx, fy), 7, (0, 255, 180), 1)

    label_x = max(6, int(round(float(outer[:, 0].min()))))
    label_y = max(20, int(round(float(outer[:, 1].min()))) - 6)
    label = f"#{tile.tile_index + 1}/{tile.total_tiles} (RS:{tile.rs_errors_corrected})"
    cv2.putText(
        annotated_bgr,
        label,
        (label_x, label_y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )


def _quad_signature(pts: np.ndarray) -> tuple[int, ...]:
    rounded = tuple(
        sorted((int(round(float(p[0]) / 8.0)), int(round(float(p[1]) / 8.0))) for p in pts)
    )
    return tuple(v for xy in rounded for v in xy)


def decode_frame(image: np.ndarray, draw_overlay: bool = False) -> FrameScanResult:
    """Detect and decode all PV-RIBBON v1 tiles visible in a BGR or grayscale frame."""
    bgr, gray = _ensure_bgr_and_gray(image)
    annotated = bgr.copy() if draw_overlay else bgr

    decoded_by_key: dict[tuple[int, int], DecodedTile] = {}
    all_candidate_quads: list[np.ndarray] = []
    tried_sigs: set[tuple[int, ...]] = set()
    claimed_centers: list[tuple[float, float, float]] = []
    decoded_bullseye_sizes: list[float] = []

    def _run_quads(quads: list[_QuadCandidate]) -> None:
        for q in quads:
            all_candidate_quads.append(q.pts)
            if claimed_centers and _shares_claimed_corners(q.pts, claimed_centers):
                continue
            sig = _quad_signature(q.pts)
            if sig in tried_sigs:
                continue
            tried_sigs.add(sig)
            decoded = _decode_quad(gray, q.pts)
            if decoded is not None:
                key = (decoded.file_id, decoded.tile_index)
                prev = decoded_by_key.get(key)
                if prev is None or decoded.rs_errors_corrected < prev.rs_errors_corrected:
                    decoded_by_key[key] = decoded
                top_e = float(np.linalg.norm(decoded.quad_pts[1] - decoded.quad_pts[0]))
                side_e = float(np.linalg.norm(decoded.quad_pts[3] - decoded.quad_pts[0]))
                b_sz = (max(top_e, side_e) / 55.0) * 7.0
                decoded_bullseye_sizes.append(b_sz)
                rad = max(12.0, 0.75 * b_sz)
                for pt in decoded.quad_pts:
                    claimed_centers.append((float(pt[0]), float(pt[1]), rad))

    def _unclaimed_plausible_count(candidates: list[_FiducialCandidate]) -> int:
        if not decoded_by_key:
            return len(candidates)
        min_b = min(decoded_bullseye_sizes) if decoded_bullseye_sizes else 14.0
        count = 0
        for cand in candidates:
            if cand.size < 0.60 * min_b:
                continue
            cx, cy = cand.center
            if any(math.hypot(cx - x, cy - y) <= r for x, y, r in claimed_centers):
                continue
            count += 1
        return count

    # Pass 1: Fast Adaptive Gaussian Thresholding + RETR_TREE
    pool, binaries = _pass1_adaptive_candidates(gray)
    if len(pool) >= 4:
        _run_quads(
            _generate_candidate_quads(
                gray,
                pool,
                allow_3corner_prediction=False,
                claimed_centers=claimed_centers,
            )
        )

    need_deeper = len(decoded_by_key) == 0 or _unclaimed_plausible_count(pool) >= 2

    # Pass 2 & Pass 3 & Pass 4: Morphological repair + CLAHE/Unsharp + 3-corner NCC
    if need_deeper:
        _pass2_morph_repair(binaries, pool)
        gray_clahe = _pass3_clahe_unsharp(gray, pool)
        if len(pool) >= 3:
            _run_quads(
                _generate_candidate_quads(
                    gray,
                    pool,
                    allow_3corner_prediction=True,
                    claimed_centers=claimed_centers,
                )
            )
    else:
        gray_clahe = None

    need_pass5 = len(decoded_by_key) == 0 or _unclaimed_plausible_count(pool) >= 2

    # Pass 5: Multi-scale rotation-invariant radial bullseye matchTemplate + NMS
    if need_pass5:
        if gray_clahe is None:
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            gray_clahe = clahe.apply(gray)
        _pass5_radial_template_candidates(gray_clahe, pool)
        if len(pool) >= 3:
            _run_quads(
                _generate_candidate_quads(
                    gray,
                    pool,
                    allow_3corner_prediction=True,
                    max_quads=36,
                    claimed_centers=claimed_centers,
                )
            )

    decoded_list = sorted(
        decoded_by_key.values(), key=lambda t: (t.file_id, t.tile_index)
    )

    if draw_overlay:
        for tile in decoded_list:
            _draw_tile_overlay(annotated, tile)

    return FrameScanResult(
        decoded_tiles=decoded_list,
        candidate_quads=all_candidate_quads,
        annotated_bgr=annotated,
    )


detect_and_decode_frame = decode_frame


def decode_single_tile(image: np.ndarray) -> Optional[DecodedTile]:
    """Decode a single PV-RIBBON v1 tile image and return the DecodedTile or None."""
    result = decode_frame(image, draw_overlay=False)
    return result.decoded_tiles[0] if result.decoded_tiles else None


# ---------------------------------------------------------------------------
# Pure-RAM Tile Assembler
# ---------------------------------------------------------------------------


class TileAssembler:
    """Thread-safe RAM collector that locks onto a file_id and reassembles tiles."""

    def __init__(self, auto_reset_on_new_file: bool = False) -> None:
        self._auto_reset_on_new_file = bool(auto_reset_on_new_file)
        self._lock = threading.Lock()
        self._file_id: Optional[int] = None
        self._total_tiles: Optional[int] = None
        self._tiles: dict[int, bytes] = {}
        self._rs_errors: dict[int, int] = {}

    @property
    def file_id(self) -> Optional[int]:
        with self._lock:
            return self._file_id

    @property
    def total_tiles(self) -> Optional[int]:
        with self._lock:
            return self._total_tiles

    @property
    def total_rs_errors(self) -> int:
        with self._lock:
            return sum(self._rs_errors.values())

    @property
    def max_rs_errors(self) -> int:
        with self._lock:
            return max(self._rs_errors.values()) if self._rs_errors else 0

    @property
    def progress(self) -> tuple[int, int]:
        with self._lock:
            return (len(self._tiles), self._total_tiles or 0)

    @property
    def is_complete(self) -> bool:
        with self._lock:
            return (
                self._total_tiles is not None
                and len(self._tiles) == self._total_tiles
            )

    def missing_indices(self) -> list[int]:
        with self._lock:
            if self._total_tiles is None:
                return []
            return [i for i in range(self._total_tiles) if i not in self._tiles]

    def reset(self) -> None:
        with self._lock:
            self._file_id = None
            self._total_tiles = None
            self._tiles.clear()
            self._rs_errors.clear()

    def add_tile(self, tile: DecodedTile) -> bool:
        """Store a decoded tile in RAM. Returns True if newly added."""
        if tile.total_tiles < 1 or not (0 <= tile.tile_index < tile.total_tiles):
            return False

        with self._lock:
            if self._file_id is None:
                self._file_id = tile.file_id
                self._total_tiles = tile.total_tiles
            elif tile.file_id != self._file_id or tile.total_tiles != self._total_tiles:
                if not self._auto_reset_on_new_file:
                    return False
                self._file_id = tile.file_id
                self._total_tiles = tile.total_tiles
                self._tiles.clear()
                self._rs_errors.clear()

            if tile.tile_index in self._tiles:
                return False

            self._tiles[tile.tile_index] = bytes(tile.payload)
            self._rs_errors[tile.tile_index] = int(tile.rs_errors_corrected)
            return True

    def assemble(self) -> codec.RecoveredFile:
        """Reassemble all collected tiles in RAM, decompress, and verify SHA-256."""
        with self._lock:
            if self._total_tiles is None or len(self._tiles) != self._total_tiles:
                missing = (
                    []
                    if self._total_tiles is None
                    else [i for i in range(self._total_tiles) if i not in self._tiles]
                )
                raise ValueError(
                    f"Cannot assemble incomplete tile set; missing indices={missing}"
                )
            chunks = [self._tiles[i] for i in range(self._total_tiles)]

        container = b"".join(chunks)
        return codec.unpack_file_container(container)


# ---------------------------------------------------------------------------
# Real-Time OpenCV Webcam Scanner with Live HUD
# ---------------------------------------------------------------------------


def _draw_webcam_hud(
    frame_bgr: np.ndarray,
    assembler: TileAssembler,
    complete: bool,
) -> np.ndarray:
    """Render live progress bar, missing chunks, RS stats, and key hints onto BGR frame."""
    out = frame_bgr
    h, w = out.shape[:2]
    bar_h = 84
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (w, min(h, bar_h)), (18, 22, 28), -1)
    cv2.addWeighted(overlay, 0.78, out, 0.22, 0, out)

    got, total = assembler.progress
    missing = assembler.missing_indices()
    rs_total = assembler.total_rs_errors
    fid = assembler.file_id

    if total > 0:
        status_text = f"Da hut {got}/{total} manh"
        if fid is not None:
            status_text += f" | ID:{fid:08X}"
        if complete:
            status_text += " [HOAN TAT!]"
    else:
        status_text = "Da hut 0/0 manh | Dang tim khung PV-RIBBON..."

    cv2.putText(
        out,
        status_text,
        (14, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 140) if complete else (240, 245, 250),
        2,
        cv2.LINE_AA,
    )

    # Progress bar
    bar_x0, bar_y0 = 14, 36
    bar_w = max(120, min(w - 28, 420))
    cv2.rectangle(out, (bar_x0, bar_y0), (bar_x0 + bar_w, bar_y0 + 14), (70, 75, 85), -1)
    if total > 0:
        fill_w = int(round(bar_w * (got / total)))
        bar_color = (0, 235, 120) if complete else (0, 210, 255)
        if fill_w > 0:
            cv2.rectangle(
                out, (bar_x0, bar_y0), (bar_x0 + fill_w, bar_y0 + 14), bar_color, -1
            )
    cv2.rectangle(out, (bar_x0, bar_y0), (bar_x0 + bar_w, bar_y0 + 14), (180, 190, 200), 1)

    # Missing chunk indices + RS error total + controls
    if total > 0:
        if missing:
            preview = ", ".join(str(i + 1) for i in missing[:10])
            if len(missing) > 10:
                preview += f", ... (+{len(missing) - 10})"
            miss_str = f"Thieu: [{preview}]"
        else:
            miss_str = "Thieu: []"
    else:
        miss_str = "Thieu: --"

    info_line = f"{miss_str} | RS da sua: {rs_total}B | [R] Reset  [Q/ESC] Quit"
    cv2.putText(
        out,
        info_line,
        (14, 72),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (205, 220, 235),
        1,
        cv2.LINE_AA,
    )
    return out


class WebcamScanner:
    """Real-time cv2.VideoCapture scanner with live HUD and headless/GUI callbacks."""

    def __init__(
        self,
        camera_index: int | str = 0,
        window_name: str = "PAPER-VOID Optical Scanner",
        show_window: bool = True,
        assembler: Optional[TileAssembler] = None,
        on_progress: Optional[
            Callable[[int, int, list[int], int, Optional[int]], None]
        ] = None,
        on_frame: Optional[Callable[[FrameScanResult], None]] = None,
        stop_event: Optional[threading.Event] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        capture_width: int = 1280,
        capture_height: int = 720,
        auto_close_delay_ms: int = 500,
    ) -> None:
        if isinstance(camera_index, str):
            stripped = camera_index.strip()
            self.camera_index: int | str = int(stripped) if stripped.isdigit() else stripped
        else:
            self.camera_index = int(camera_index)
        self.window_name = window_name
        self.show_window = bool(show_window)
        self.assembler = assembler if assembler is not None else TileAssembler()
        self.on_progress = on_progress
        self.on_frame = on_frame
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.should_cancel = should_cancel
        self.capture_width = int(capture_width)
        self.capture_height = int(capture_height)
        self.auto_close_delay_ms = max(0, int(auto_close_delay_ms))

    def stop(self) -> None:
        self.stop_event.set()

    def _is_cancelled(self) -> bool:
        if self.stop_event.is_set():
            return True
        if self.should_cancel is not None and self.should_cancel():
            return True
        return False

    def _open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.camera_index)
        if isinstance(self.camera_index, int) and not cap.isOpened() and hasattr(cv2, "CAP_DSHOW"):
            cap.release()
            cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Unable to open camera source {self.camera_index!r}")
        if isinstance(self.camera_index, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.capture_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.capture_height)
        return cap

    def run(self, timeout_sec: Optional[float] = None) -> Optional[codec.RecoveredFile]:
        """Run webcam decode loop until complete (returns RecoveredFile) or cancelled (None)."""
        cap = self._open_camera()
        start_time = time.monotonic()
        last_progress_sig: Optional[tuple[int, int, int, Optional[int]]] = None
        window_created = False

        try:
            while not self._is_cancelled():
                if timeout_sec is not None and (time.monotonic() - start_time) >= timeout_sec:
                    return None

                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    if self.show_window:
                        key = cv2.waitKey(10) & 0xFF
                        if key in (ord("q"), ord("Q"), 27):
                            return None
                    else:
                        time.sleep(0.01)
                    continue

                scan_res = decode_frame(frame, draw_overlay=True)
                added_any = False
                for tile in scan_res.decoded_tiles:
                    if self.assembler.add_tile(tile):
                        added_any = True

                got, total = self.assembler.progress
                rs_total = self.assembler.total_rs_errors
                fid = self.assembler.file_id
                sig = (got, total, rs_total, fid)

                if added_any or sig != last_progress_sig:
                    last_progress_sig = sig
                    if self.on_progress is not None:
                        self.on_progress(
                            got, total, self.assembler.missing_indices(), rs_total, fid
                        )

                complete = self.assembler.is_complete
                _draw_webcam_hud(scan_res.annotated_bgr, self.assembler, complete=complete)

                if self.on_frame is not None:
                    self.on_frame(scan_res)

                if self.show_window:
                    cv2.imshow(self.window_name, scan_res.annotated_bgr)
                    window_created = True
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("r"), ord("R")):
                        self.assembler.reset()
                        last_progress_sig = None
                        if self.on_progress is not None:
                            self.on_progress(0, 0, [], 0, None)
                    elif key in (ord("q"), ord("Q"), 27):
                        return None
                    elif (
                        cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE)
                        < 1
                    ):
                        return None

                if complete:
                    recovered = self.assembler.assemble()
                    if self.show_window and self.auto_close_delay_ms > 0:
                        cv2.waitKey(self.auto_close_delay_ms)
                    return recovered

            return None
        finally:
            cap.release()
            if window_created:
                try:
                    cv2.destroyWindow(self.window_name)
                except cv2.error:
                    pass


# ---------------------------------------------------------------------------
# Offline Phone Photo & Video Scanner (No PC Webcam Required)
# ---------------------------------------------------------------------------

_VIDEO_EXTS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}
)


def _load_image_unicode(path: str) -> Optional[np.ndarray]:
    """Read an image file into BGR ndarray, supporting Vietnamese/Unicode Windows paths."""
    try:
        raw = np.fromfile(path, dtype=np.uint8)
        if raw.size == 0:
            return None
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _decode_photo_multi_scale(img_bgr: np.ndarray) -> FrameScanResult:
    """Decode a phone photo, downscaling very large images (>1600px) first for speed."""
    h, w = img_bgr.shape[:2]
    max_dim = max(h, w)
    if max_dim > 1600:
        scale = 1400.0 / float(max_dim)
        resized = cv2.resize(
            img_bgr,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
        res = decode_frame(resized, draw_overlay=False)
        if res.decoded_tiles:
            return res
    return decode_frame(img_bgr, draw_overlay=False)


def scan_media_files(
    paths: Iterable[str],
    *,
    assembler: Optional[TileAssembler] = None,
    video_frame_stride: int = 3,
    on_progress: Optional[Callable[[int, int, list[int], int, Optional[int]], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> tuple[Optional[codec.RecoveredFile], TileAssembler]:
    """
    Scan PV-RIBBON v1 tiles from phone photos (.jpg/.png/...) and/or videos (.mp4/.mov/...).
    Returns `(RecoveredFile or None, assembler)`.
    """
    asm = assembler if assembler is not None else TileAssembler()
    stride = max(1, int(video_frame_stride))

    for raw_path in paths:
        if stop_event is not None and stop_event.is_set():
            break
        ext = raw_path.lower().rsplit(".", 1)[-1] if "." in raw_path else ""
        is_video = f".{ext}" in _VIDEO_EXTS

        if is_video:
            cap = cv2.VideoCapture(raw_path)
            if not cap.isOpened():
                cap.release()
                continue
            try:
                frame_idx = 0
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break
                    frame_idx += 1
                    if (frame_idx - 1) % stride != 0:
                        continue
                    res = _decode_photo_multi_scale(frame)
                    added = False
                    for tile in res.decoded_tiles:
                        if asm.add_tile(tile):
                            added = True
                    if added and on_progress is not None:
                        got, tot = asm.progress
                        on_progress(got, tot, asm.missing_indices(), asm.total_rs_errors, asm.file_id)
                    if asm.is_complete:
                        return asm.assemble(), asm
            finally:
                cap.release()
        else:
            img = _load_image_unicode(raw_path)
            if img is None:
                continue
            res = _decode_photo_multi_scale(img)
            added = False
            for tile in res.decoded_tiles:
                if asm.add_tile(tile):
                    added = True
            if added and on_progress is not None:
                got, tot = asm.progress
                on_progress(got, tot, asm.missing_indices(), asm.total_rs_errors, asm.file_id)
            if asm.is_complete:
                return asm.assemble(), asm

    return (asm.assemble() if asm.is_complete else None), asm


def decode_key_card_from_media(
    paths: Iterable[str],
    *,
    video_frame_stride: int = 4,
    stop_event: Optional[threading.Event] = None,
) -> Optional[DecodedTile]:
    """Scan phone photos or videos for a single 1-tile PVK1 Paper-Key Card."""
    stride = max(1, int(video_frame_stride))
    for raw_path in paths:
        if stop_event is not None and stop_event.is_set():
            return None
        ext = raw_path.lower().rsplit(".", 1)[-1] if "." in raw_path else ""
        is_video = f".{ext}" in _VIDEO_EXTS
        if is_video:
            cap = cv2.VideoCapture(raw_path)
            if not cap.isOpened():
                cap.release()
                continue
            try:
                idx = 0
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return None
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break
                    idx += 1
                    if (idx - 1) % stride != 0:
                        continue
                    res = _decode_photo_multi_scale(frame)
                    for tile in res.decoded_tiles:
                        if tile.payload.startswith(b"PVK1"):
                            return tile
            finally:
                cap.release()
        else:
            img = _load_image_unicode(raw_path)
            if img is None:
                continue
            res = _decode_photo_multi_scale(img)
            for tile in res.decoded_tiles:
                if tile.payload.startswith(b"PVK1"):
                    return tile
    return None


__all__ = [
    "ASPECT_55_39",
    "DIAG_CELLS",
    "DST_FIDUCIAL_PTS",
    "DST_OUTER_CORNERS",
    "DecodedTile",
    "FrameScanResult",
    "TileAssembler",
    "WARP_CELL",
    "WARP_H",
    "WARP_W",
    "WebcamScanner",
    "decode_frame",
    "decode_key_card_from_media",
    "decode_single_tile",
    "detect_and_decode_frame",
    "scan_media_files",
]
