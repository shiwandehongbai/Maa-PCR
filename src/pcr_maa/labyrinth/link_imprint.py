"""Link-imprint attribute identity from the element badge, not the card-name OCR.

The three-offer page used to read the attribute from the ``X属性的随机印记``
text line. OCR of that line is a locale form and can be split or misread while
the drawn card itself is stable, so identity is now the game's element badge in
the card's lower-right corner. The five badges are versioned resources:

* ``link-imprint-pack/PROVENANCE.json`` is the pack manifest: five 44x44 RGBA
  PNGs addressed relatively and pinned by sha256, byte count, dimensions and
  pixel format, with their element, source id and crop rectangle.
* Every icon is the badge sprite cropped from
  ``icon_labyrinth_icon_summon_unit_<id>.png`` at (84,84,44,44). The 31xxxxxx
  series is the one drawn on the live card (NCC 0.983-0.987 on the two
  2026-11-09 frames); the 11xxxxxx badges only reached 0.886-0.903 there and
  are not used.

Matching samples the badge at the fixed 720p card geometry. The badge is drawn
at 55px from the 44px art (1.25x), so a small scale/offset search is used; the
score is the masked colour NCC of the whole crop. Identity is reported only
when the best element and the rival margin both pass. A missing or corrupt
pack is a resource error, never a guessed element; the caller decides whether
the page is usable at all.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import math
import re

import cv2
import numpy as np
from maa.custom_recognition import CustomRecognition

from .batch import BatchStopped

DATA = Path(__file__).resolve().parents[3] / 'assets/labyrinth'
PACK = DATA / 'link-imprint-pack'
SCHEMA_VERSION = 1
SIZE = 44
# The same five attributes the upstream priority uses: fire > dark > light >
# wind > water. Keeping one tuple means the pack and the policy cannot drift.
ELEMENTS = ('火', '暗', '光', '风', '水')
# The drawn badge box at 1280x720: 44px art at 1.25. The search ROI leaves a
# few pixels around it for rounding, and the scales cover a 2px resize drift.
BADGE_ROIS = tuple((offset + 160, 346, 55, 55) for offset in (135, 505, 875))
SEARCH_ROIS = tuple((offset + 155, 341, 65, 65) for offset in (135, 505, 875))
TEMPLATE_SIZES = (53, 54, 55, 56, 57)
CONFIDENCE = .85
RIVAL_MARGIN = .15
CANDIDATES = 5
_SHA256 = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class ImprintMatch:
    element: str | None
    score: float
    rival: float
    margin: float
    trusted: bool
    candidates: tuple
    art: tuple | None = None


def _changed():
    raise BatchStopped('link_imprint_resource_changed')


def _frame(value):
    return (type(value) is np.ndarray and value.dtype == np.uint8 and value.ndim == 3
            and value.shape[:2] == (720, 1280) and value.shape[2] >= 3)


def _text(value):
    if type(value) is not str or not value.strip():
        _changed()
    return value


def _count(value, *, minimum=1):
    if type(value) is not int or value < minimum:
        _changed()
    return value


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        _changed()


def _resolve(value, base):
    if type(value) is not str:
        _changed()
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or any(part in ('..', '.') for part in relative.parts):
        _changed()
    return base.joinpath(*relative.parts)


class ImprintCatalog:
    """Validated element-badge templates; immutable after construction."""

    def __init__(self, pack=PACK):
        self.pack_path = Path(pack)
        try:
            self._load()
        except BatchStopped:
            raise
        except Exception:
            raise BatchStopped('link_imprint_resource_changed') from None

    def _load(self):
        provenance = _read_json(self.pack_path / 'PROVENANCE.json')
        if (type(provenance) is not dict
                or type(provenance.get('schemaVersion')) is not int
                or provenance['schemaVersion'] != SCHEMA_VERSION):
            _changed()
        rows = provenance.get('icons')
        if type(rows) is not list or not rows:
            _changed()
        images = {}
        for row in rows:
            if type(row) is not dict:
                _changed()
            element = row.get('element')
            if type(element) is not str or element not in ELEMENTS or element in images:
                _changed()
            path = _resolve(row.get('path'), self.pack_path)
            digest = row.get('sha256')
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                _changed()
            if (_count(row.get('width')) != SIZE or _count(row.get('height')) != SIZE
                    or row.get('pixelFormat') != '32bppArgb'):
                _changed()
            try:
                blob = path.read_bytes()
            except OSError:
                _changed()
            if _count(row.get('bytes')) != len(blob) or hashlib.sha256(blob).hexdigest() != digest:
                _changed()
            image = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if type(image) is not np.ndarray or image.dtype != np.uint8 or image.shape != (SIZE, SIZE, 4):
                _changed()
            images[element] = image[:, :, :3]
        # The manifest must name exactly the five attributes, no fallback icon.
        if set(images) != set(ELEMENTS):
            _changed()
        self._templates = tuple((element, images[element]) for element in ELEMENTS)
        self._prepare()

    def _prepare(self):
        scaled = {}
        for element, image in self._templates:
            scaled[element] = tuple((size, cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA))
                                    for size in TEMPLATE_SIZES)
        self._scaled = scaled

    def match(self, frame, slot):
        """Identify one card's badge; ``element`` is set only when trusted."""
        if not _frame(frame):
            raise BatchStopped('link_frame_unknown')
        if type(slot) is not int or slot not in (0, 1, 2):
            raise BatchStopped('link_frame_unknown')
        x, y, width, height = SEARCH_ROIS[slot]
        region = frame[y:y + height, x:x + width]
        if region.shape[0] < max(TEMPLATE_SIZES) or region.shape[1] < max(TEMPLATE_SIZES):
            raise BatchStopped('link_frame_unknown')
        scores, rects = {}, {}
        for element, images in self._scaled.items():
            best, best_rect = -2., None
            for size, template in images:
                result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
                if result.size == 0:
                    continue
                flat = int(np.argmax(result))
                left, top = flat % result.shape[1], flat // result.shape[1]
                value = float(result[top, left])
                if not math.isfinite(value):
                    value = 0.
                if value > best:
                    best, best_rect = value, (x + left, y + top, size, size)
            scores[element] = max(-1., min(1., best))
            rects[element] = best_rect
        order = sorted(ELEMENTS, key=lambda element: (-scores[element], ELEMENTS.index(element)))
        best_element = order[0]
        score = scores[best_element]
        rival = scores[order[1]] if len(order) > 1 else 0.
        margin = score - rival
        trusted = score >= CONFIDENCE and margin >= RIVAL_MARGIN
        candidates = tuple((element, round(scores[element], 4)) for element in order[:CANDIDATES])
        return ImprintMatch(trusted and best_element or None, score, rival, margin,
                            trusted, candidates, rects[best_element])


def _detail(match):
    return dict(element=match.element, trusted=match.trusted, score=round(float(match.score), 4),
                rival=round(float(match.rival), 4), margin=round(float(match.margin), 4),
                candidates=[[element, score] for element, score in match.candidates],
                art=None if match.art is None else list(match.art))


@lru_cache(maxsize=8)
def _load(pack):
    return ImprintCatalog(pack)


def load_catalog(pack=PACK):
    """One validated badge pack per process; a bad pack stops at load time."""
    try:
        key = Path(pack).resolve()
    except (TypeError, ValueError):
        _changed()
    return _load(key)


LINK_ICON = 'LabyLinkImprintIconCapture'


class LinkImprintIconCapture(CustomRecognition):
    """Read-only badge identity for the offers observation and the fresh guard.

    ``offers`` always returns a hit so an ``And`` page observation stays
    stable; ``trusted`` decides whether callers may use the element. The
    ``guard`` mode only hits when this frame's badge equals the intended
    element, so the click proof never depends on the card-name OCR.
    """

    def __init__(self, catalog=None):
        super().__init__()
        self.catalog = catalog

    def _miss(self, detail):
        return self.AnalyzeResult(None, detail)

    def analyze(self, context, argv):
        try:
            params = json.loads(argv.custom_recognition_param or '{}')
        except (TypeError, ValueError):
            params = {}
        if type(params) is not dict:
            params = {}
        mode = params.get('mode')
        miss = None if mode == 'guard' else [0, 0, 1280, 720]
        try:
            if self.catalog is None:
                return self.AnalyzeResult(miss, dict(trusted=False, reason='resource_missing'))
            if not _frame(argv.image):
                return self.AnalyzeResult(miss, dict(trusted=False, reason='frame_unknown'))
            if mode == 'offers':
                slots = [_detail(self.catalog.match(argv.image, slot)) for slot in range(3)]
                return self.AnalyzeResult([0, 0, 1280, 720],
                                          dict(mode='offers', slots=slots,
                                               trusted=all(slot['trusted'] for slot in slots)))
            if mode == 'guard':
                slot, expected = params.get('slot'), params.get('element')
                if type(slot) is not int or slot not in (0, 1, 2) or expected not in ELEMENTS:
                    return self._miss(dict(trusted=False, matched=False, reason='guard_param_unknown'))
                match = self.catalog.match(argv.image, slot)
                matched = bool(match.trusted and match.element == expected)
                detail = dict(_detail(match), mode='guard', slot=slot, expected=expected, matched=matched)
                return self.AnalyzeResult(list(BADGE_ROIS[slot]) if matched else None, detail)
            return self.AnalyzeResult(miss, dict(trusted=False, reason='mode_unknown'))
        except BatchStopped as error:
            return self.AnalyzeResult(miss, dict(trusted=False, reason=str(error.args[0])))
        except Exception:
            return self.AnalyzeResult(miss, dict(trusted=False, reason='recognition_error'))
