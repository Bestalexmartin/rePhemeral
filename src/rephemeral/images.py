"""Turn an arbitrary user image into something safe to write to the device.

Two constraints drive this module, both measured rather than assumed.

The panel is 1620x2160 for full-screen art and 1012x1012 for the sleep
carousel, so anything else has to be resized, and the resize has to make a
deliberate choice about aspect ratio rather than silently distorting.

The rootfs has roughly 41 MB free. Stock screens are 20-190 KB because
they are flat vector-ish art. A photograph at panel size in RGBA can
encode to several MB, and a handful of those fills the partition the
tablet boots from. So encoding is not "save as PNG": it is a search for
the smallest honest encoding that stays under the cap, escalating from
plain compression through palette quantisation, and failing loudly rather
than writing something oversized.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageOps

from .device import MAX_IMAGE_BYTES
from .screens import Screen

#: Fit strategies for reconciling the source aspect ratio with the panel.
FIT_MODES = ("cover", "contain", "stretch")

#: Palette sizes tried, in order, when plain compression is too large.
_PALETTE_STEPS = (256, 128, 64, 32, 16)


class ImageError(ValueError):
    pass


@dataclass
class Prepared:
    """An image converted and encoded ready for the device."""

    data: bytes
    width: int
    height: int
    mode: str
    notes: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @property
    def size_kb(self) -> float:
        return len(self.data) / 1024


def _load(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        if data[:512].lstrip()[:5].lower().startswith(b"<?xml") or b"<svg" in data[:512].lower():
            raise ImageError(
                "that looks like an SVG. Pillow only reads raster formats, so "
                "export it to PNG at 1620x2160 (or larger) first and use that."
            ) from exc
        raise ImageError(f"could not read that file as an image: {exc}") from exc
    # Honour EXIF rotation before anything else, or portrait photos from a
    # phone arrive sideways and the crop is computed on the wrong axis.
    return ImageOps.exif_transpose(img)


def _flatten(img: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Composite any transparency onto `background`, then return RGB.

    `Image.convert("RGB")` discards the alpha channel rather than
    compositing it, so transparent pixels keep whatever RGB the exporter
    happened to leave underneath: white for some tools, black for others,
    fringing for others again. A logo is typically more than half
    transparent, so the difference is the whole image, and the failure is
    silent. Compositing explicitly makes the background option mean what
    it says.
    """
    if img.mode == "P":
        img = img.convert("RGBA") if "transparency" in img.info else img.convert("RGB")
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (*background, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return img.convert("RGB")


def _resize(img: Image.Image, w: int, h: int, fit: str,
            background: tuple[int, int, int]) -> tuple[Image.Image, list[str]]:
    notes: list[str] = []
    if fit not in FIT_MODES:
        raise ImageError(f"unknown fit mode {fit!r}; expected one of {FIT_MODES}")

    src_ratio = img.width / img.height
    dst_ratio = w / h

    if fit == "stretch":
        if abs(src_ratio - dst_ratio) > 0.01:
            notes.append("stretched to fit; the image is distorted")
        return _flatten(img, background).resize((w, h), Image.LANCZOS), notes

    if fit == "cover":
        out = ImageOps.fit(_flatten(img, background), (w, h), method=Image.LANCZOS,
                           centering=(0.5, 0.5))
        if abs(src_ratio - dst_ratio) > 0.01:
            notes.append("cropped to fill the panel; edges are outside the frame")
        return out, notes

    # contain: scale to fit entirely, pad the remainder
    scaled = ImageOps.contain(_flatten(img, background), (w, h), method=Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), background)
    canvas.paste(scaled, ((w - scaled.width) // 2, (h - scaled.height) // 2))
    if abs(src_ratio - dst_ratio) > 0.01:
        notes.append(f"letterboxed onto a {background} background")
    return canvas, notes


def _encode(img: Image.Image, cap: int) -> tuple[bytes, str, list[str]]:
    """Encode as PNG under `cap` bytes, escalating compression.

    Returns (data, mode_description, notes). Raises if even the most
    aggressive step is too large, rather than returning something that
    would not fit on the device.
    """
    notes: list[str] = []

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    data = buf.getvalue()
    if len(data) <= cap:
        return data, "RGB", notes

    notes.append(
        f"full-colour encoding was {len(data) / 1024 / 1024:.1f} MB, "
        f"over the {cap / 1024 / 1024:.0f} MB cap; reducing colours"
    )
    for colours in _PALETTE_STEPS:
        quant = img.quantize(colors=colours, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
        buf = io.BytesIO()
        quant.save(buf, format="PNG", optimize=True, compress_level=9)
        data = buf.getvalue()
        if len(data) <= cap:
            notes.append(f"quantised to {colours} colours ({len(data) / 1024:.0f} KB)")
            return data, f"P{colours}", notes

    raise ImageError(
        f"this image cannot be encoded under the {cap / 1024 / 1024:.0f} MB cap "
        f"even at 16 colours. It is probably photographic noise at full panel "
        f"size. Try a simpler image, or reduce its detail before importing."
    )


def prepare(
    data: bytes,
    screen: Screen,
    fit: str = "cover",
    background: tuple[int, int, int] = (255, 255, 255),
    greyscale: bool = False,
    cap: int = MAX_IMAGE_BYTES,
) -> Prepared:
    """Convert `data` into a PNG sized and encoded for `screen`."""
    img = _load(data)
    original = f"{img.width}x{img.height}"

    resized, notes = _resize(img, screen.width, screen.height, fit, background)

    if greyscale:
        resized = resized.convert("L").convert("RGB")
        notes.append("converted to greyscale")

    encoded, mode, enc_notes = _encode(resized, cap)
    notes.extend(enc_notes)

    if original != f"{screen.width}x{screen.height}":
        notes.insert(0, f"resized from {original} to {screen.width}x{screen.height}")

    return Prepared(
        data=encoded,
        width=screen.width,
        height=screen.height,
        mode=mode,
        notes=notes,
    )


def thumbnail(data: bytes, max_edge: int = 320) -> bytes:
    """Small PNG preview, for the UI. Never written to the device."""
    img = _flatten(_load(data), (255, 255, 255))
    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
