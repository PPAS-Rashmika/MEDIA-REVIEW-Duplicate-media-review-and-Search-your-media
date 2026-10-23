"""Explicit orientation and bounded crop comparisons for optional image suggestions."""
from __future__ import annotations

from PIL import Image, ImageChops, ImageStat


def _hash(image):
    values = list(image.convert('L').resize((9, 8), Image.Resampling.LANCZOS).tobytes())
    result = 0
    for y in range(8):
        for x in range(8):
            result = (result << 1) | (values[y * 9 + x] > values[y * 9 + x + 1])
    return result


def similarity(left, right, threshold, rotations=False, crops=False, should_stop=None):
    if not left.transformSample or not right.transformSample:
        return None
    first = Image.frombytes('RGB', (64, 64), left.transformSample)
    target = Image.frombytes('RGB', (64, 64), right.transformSample)
    variants = [('original', first)]
    if rotations:
        variants += [('rotated 90 degrees', first.transpose(Image.Transpose.ROTATE_90)),
                     ('rotated 180 degrees', first.transpose(Image.Transpose.ROTATE_180)),
                     ('rotated 270 degrees', first.transpose(Image.Transpose.ROTATE_270))]
        variants += [('flipped ' + name, image.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
                     for name, image in list(variants)]
    best = None
    target_hash = _hash(target)
    # Crop/resample boundaries can change edge bits even when RGB agreement is
    # very high. The independent colour-distance gate remains mandatory.
    limit = 12 if crops else max(3, min(12, round((100 - threshold) * 1.2)))

    def compare(image, reference, reference_hash, reason):
        nonlocal best
        image = image.resize((64, 64), Image.Resampling.LANCZOS)
        if (_hash(image) ^ reference_hash).bit_count() > limit:
            return
        difference = ImageStat.Stat(ImageChops.difference(image, reference))
        score = 100 * (1 - sum(difference.mean) / (3 * 255))
        if score >= threshold and max(difference.mean) <= max(7, (100 - threshold) * 1.6):
            if best is None or score > best[0]:
                best = (round(score, 1), reason)

    for name, image in variants:
        if should_stop and should_stop():
            return best
        compare(image, target, target_hash, name)
        if not crops:
            continue
        # Search a finite grid of crop windows in both directions. The UI describes
        # this as bounded crop matching, not arbitrary object/subimage detection.
        for source, reference, direction in ((image, target, 'first image'), (target, image, 'second image')):
            reference_hash = _hash(reference)
            for width in (64, 51, 42):
                for height in (64, 51, 42):
                    if width == height == 64:
                        continue
                    for x in set((0, (64 - width) // 2, 64 - width)):
                        for y in set((0, (64 - height) // 2, 64 - height)):
                            if should_stop and should_stop():
                                return best
                            compare(source.crop((x, y, x + width, y + height)), reference,
                                    reference_hash, f'{name}; cropped {direction}')
    return best
