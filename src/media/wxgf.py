"""Unwrap WeChat's ``wxgf`` image container into a viewable image.

WeChat 4.x stores some images as an HEVC stream inside a small container
whose first four bytes are ``wxgf``.  The container is not a normal image
file — nothing renders it directly — but it is *not* encrypted either, which
makes it the one path to a real picture without the image keys.

Structure, measured on 12 real files::

    "wxgf" <12 bytes of metadata> [00 00 00 01 <VPS>] [SPS] [PPS] [IDR]

Some files embed a plain JPEG or PNG after the header instead, and some
``_hd_temp`` files are a plain PNG with no container at all.

Decoding needs an HEVC codec.  :mod:`av` (PyAV, which bundles FFmpeg) is
used when importable; it is deliberately excluded from the packaged build to
keep the EXE small, so in the frozen app this module reports "unavailable"
and the caller falls back to ``[图片]``.  Same trade-off as voice
recognition — see ``build.spec``.

These files are written by WeChat only after the user opens the image in the
client, so this path cannot see every image, only the ones already viewed.
"""

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MAGIC = b"wxgf"

#: A frame smaller than this is a placeholder, not a picture.
_MIN_JPEG_BYTES = 512

#: Only the head can hold an embedded image; scanning further is wasted work.
_EMBEDDED_SCAN_LIMIT = 4096

# NALU types worth keeping: 32=VPS, 33=SPS, 34=PPS, 19/20=IDR, 1=non-IDR slice.
_NALU_VPS = 32
_NALU_VCL = frozenset({1, 19, 20})

#: Formats a caller can use as-is.  ``_hd_temp`` files are often a bare PNG
#: with no ``wxgf`` wrapper at all.
_PLAIN_IMAGE_MAGICS = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"BM",
)


def is_wxgf(data: bytes) -> bool:
    """True when *data* starts with the ``wxgf`` container magic."""
    return bool(data) and data[:4] == MAGIC


def is_plain_image(data: bytes) -> bool:
    """True when *data* already is a browser-decodable image."""
    if not data:
        return False
    for magic in _PLAIN_IMAGE_MAGICS:
        if data.startswith(magic):
            return True
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def av_available() -> bool:
    """True when PyAV can be imported, i.e. HEVC decoding is possible."""
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def unwrap_wxgf(data: bytes) -> Optional[bytes]:
    """Return usable image bytes for a ``wxgf`` payload, or None.

    A payload that already is an image (JPEG/PNG/...) is returned unchanged,
    so the caller can pass any decrypted bytes through this function.

    Returns:
        JPEG (or original image) bytes, or None when the container cannot be
        decoded — no codec, no NALUs, or a decode error.
    """
    if not data:
        return None

    if is_plain_image(data):
        return data

    embedded = _find_embedded_image(data)
    if embedded is not None:
        return embedded

    if not is_wxgf(data):
        # Already an image the caller can use, or something unrecognisable.
        return None

    if not av_available():
        logger.info(
            "wxgf image needs PyAV (not bundled) — falling back to placeholder"
        )
        return None

    for name, stream in _hevc_candidates(data):
        jpeg = _decode_first_frame(stream)
        if jpeg:
            logger.debug("wxgf decoded via %s (%d bytes)", name, len(jpeg))
            return jpeg
    logger.debug("wxgf: no candidate stream decoded")
    return None


def _find_embedded_image(data: bytes) -> Optional[bytes]:
    """Return a JPEG/PNG embedded after the container header, if any."""
    limit = min(len(data) - 12, _EMBEDDED_SCAN_LIMIT)
    for i in range(4, max(limit, 4)):
        if data[i:i + 3] == b"\xff\xd8\xff":
            return data[i:]
        if data[i:i + 8] == b"\x89PNG\r\n\x1a\n":
            return data[i:]
    return None


def _extract_nalu_units(data: bytes) -> list:
    """Split *data* into NALU payloads, dropping start codes.

    Units whose first byte has the forbidden-zero bit set are dropped: they
    are header remnants rather than real NALUs, and feeding them to the
    decoder yields garbage.
    """
    starts = []
    i = 4  # skip the 4-byte magic
    end = len(data) - 3
    while i < end:
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, 4))
            i += 4
            continue
        if data[i:i + 3] == b"\x00\x00\x01":
            starts.append((i, 3))
            i += 3
            continue
        i += 1

    units = []
    for index, (pos, prefix) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        payload = data[pos + prefix:stop]
        if len(payload) < 2 or payload[0] & 0x80:
            continue
        units.append(payload)
    return units


def _nalu_type(unit: bytes) -> int:
    return (unit[0] >> 1) & 0x3F


def _hevc_candidates(data: bytes):
    """Yield ``(label, annexb_stream)`` candidates in priority order.

    A file may hold several pictures back to back, each starting with its own
    VPS.  Decoding them as one stream yields only the first picture, and
    decoding a group without its VPS fails, so groups are split on VPS.
    """
    units = _extract_nalu_units(data)
    if not units:
        return

    vps_positions = [i for i, unit in enumerate(units)
                     if _nalu_type(unit) == _NALU_VPS]
    if vps_positions:
        for order, start in enumerate(vps_positions):
            stop = (vps_positions[order + 1] if order + 1 < len(vps_positions)
                    else len(units))
            group = units[start:stop]
            if not any(_nalu_type(unit) in _NALU_VCL for unit in group):
                continue  # parameter sets without a picture
            yield f"vps_group_{order}", _annexb(group)

    yield "all_units", _annexb(units)


def _annexb(units) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + unit for unit in units)


def _decode_first_frame(stream: bytes) -> Optional[bytes]:
    """Decode the first video frame of an Annex-B HEVC stream to JPEG."""
    if not stream:
        return None
    try:
        import av
    except ImportError:  # pragma: no cover - guarded by av_available()
        return None

    try:
        with av.open(io.BytesIO(stream), format="hevc") as container:
            for frame in container.decode(video=0):
                image = frame.to_image()
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "JPEG", quality=90)
                jpeg = buffer.getvalue()
                if len(jpeg) < _MIN_JPEG_BYTES:
                    return None
                return jpeg
    except Exception:
        logger.debug("HEVC decode failed", exc_info=True)
    return None
