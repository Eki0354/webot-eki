"""Decrypt WeChat 4.x ``.dat`` image containers.

WeChat stores received images under
``msg/attach/{md5(session)}/{yyyy-MM}/Img/`` as ``.dat`` files.  The
container is **not** plain AES — it is four concatenated segments::

    [15-byte header][AES-128-ECB segment][plaintext segment][XOR segment]

Only the head of the file is encrypted.  The middle is stored verbatim and
the tail is masked with a single-byte XOR.  That is why treating the whole
payload as ciphertext never produced a valid image: the tail carries the
entropy of a JPEG, not of random bytes, and its length is not block-aligned.

Header layout (all integers little-endian)::

    offset 0   6   signature  07 08 56 31 08 07 (V1) / 07 08 56 32 08 07 (V2)
    offset 6   4   aesSize    encrypted segment length, before padding
    offset 10  4   xorSize    trailing byte count masked with ``xorKey``

V1 files are XOR-masked end to end and carry no size fields.

The keys are per machine and are **not** derivable from the message XML:
the ``aeskey`` attribute there signs CDN downloads, not local files.  They
have to be configured (see ``IMAGE_XOR_KEY`` / ``IMAGE_AES_KEY``).
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SIGNATURE_V1 = b"\x07\x08V1\x08\x07"
SIGNATURE_V2 = b"\x07\x08V2\x08\x07"

#: Bytes before the payload: 6 signature + 4 aesSize + 4 xorSize + 1 spare.
HEADER_LEN = 0x0F

_AES_BLOCK = 16

#: Container magic → extension.  ``wxgf`` is a wrapper around HEVC, handled
#: separately by :mod:`src.media.wxgf`.
_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"wxgf", "wxgf"),
)


def sniff_format(data: bytes) -> Optional[str]:
    """Return the image extension of *data*, or None if unrecognised.

    ``"wxgf"`` is returned for the HEVC container — it is not a usable image
    until :func:`src.media.wxgf.unwrap_wxgf` has run.
    """
    if not data:
        return None
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    # RIFF is shared with WAV/AVI, so the form type must match too.
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def dat_version(data: bytes) -> int:
    """Return 2 for the AES+XOR container, 1 for whole-file XOR, else 0."""
    if len(data) < 6:
        return 0
    if data[:6] == SIGNATURE_V2:
        return 2
    if data[:6] == SIGNATURE_V1:
        return 1
    return 0


def parse_xor_key(value) -> Optional[int]:
    """Normalise a configured XOR key to ``0..255``.

    Accepts ``0x37`` (hex), ``"55"`` (decimal) and plain integers, matching
    what users are likely to paste from a key extractor.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 0xFF else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        logger.debug("Unparseable image XOR key: %r", value)
        return None
    return number if 0 <= number <= 0xFF else None


def aes_key_from_text(text: str) -> Optional[bytes]:
    """Derive the 16-byte AES key from the configured value.

    The key is the **first 16 ASCII characters of the string**, not the
    hex-decoded bytes: ``"491e17ca4a5f665d4761..."`` yields
    ``b"491e17ca4a5f665d"``.  Using ``bytes.fromhex`` here silently produces
    a different key and every file fails to decrypt.
    """
    if not text:
        return None
    raw = str(text).strip().encode("ascii", "ignore")
    if len(raw) < _AES_BLOCK:
        return None
    return raw[:_AES_BLOCK]


def expected_dat_size(plain_length: int, aes_size: int = 1024) -> int:
    """Size of a V2 container holding *plain_length* bytes of image.

    Used to pair a message with its file on disk: the message XML records the
    decrypted length, and the container adds a fixed overhead.  The overhead
    is the 15-byte header plus the PKCS7 block, which is
    ``16 - aes_size % 16`` and therefore 16 when *aes_size* is already a
    multiple of 16.  With the common ``aes_size=1024`` this reduces to the
    ``file_size == length + 31`` rule that pairs 28 of 56 samples exactly.
    """
    pad = _AES_BLOCK - aes_size % _AES_BLOCK if aes_size else 0
    return HEADER_LEN + plain_length + pad


def decrypt_dat(
    data: bytes,
    xor_key: Optional[int] = None,
    aes_key_text: Optional[str] = None,
) -> Optional[bytes]:
    """Decrypt a ``.dat`` payload into raw image bytes.

    Args:
        data: Whole file contents.
        xor_key: Single-byte XOR key, or None when only V2 head decryption
            is needed (the tail then stays masked).
        aes_key_text: Configured AES key string; its first 16 ASCII bytes
            are the key.

    Returns:
        The decrypted bytes, or None when the container is unknown or a
        required key is missing.
    """
    version = dat_version(data)
    if version == 1:
        if xor_key is None:
            return None
        return bytes(byte ^ xor_key for byte in data)
    if version != 2:
        logger.debug("Unknown .dat signature: %r", data[:6])
        return None

    if len(data) < HEADER_LEN:
        return None
    header = data[:HEADER_LEN]
    payload = data[HEADER_LEN:]

    aes_size = int.from_bytes(header[6:10], "little", signed=True)
    xor_size = int.from_bytes(header[10:14], "little", signed=True)
    if aes_size < 0 or xor_size < 0:
        logger.debug("Negative segment size (aes=%d xor=%d)", aes_size, xor_size)
        return None

    # PKCS7 always adds at least one block, so an exact multiple still grows.
    # A zero-length segment means "no AES", not "one empty block".
    aligned_aes = aes_size + (_AES_BLOCK - aes_size % _AES_BLOCK) if aes_size else 0
    if aligned_aes > len(payload):
        logger.debug("aesSize %d exceeds payload %d", aligned_aes, len(payload))
        return None

    plain_head = b""
    if aes_size:
        key = aes_key_from_text(aes_key_text or "")
        if key is None:
            logger.debug("V2 container needs IMAGE_AES_KEY — not configured")
            return None
        decrypted = _aes_ecb_decrypt(payload[:aligned_aes], key)
        if decrypted is None:
            return None
        unpadded = _strip_pkcs7(decrypted)
        if unpadded is None:
            logger.debug("AES segment did not carry valid PKCS7 padding")
            return None
        plain_head = unpadded

    remaining = payload[aligned_aes:]
    if xor_size > len(remaining):
        logger.debug("xorSize %d exceeds remaining %d", xor_size, len(remaining))
        return None

    raw_len = len(remaining) - xor_size
    plain_body = remaining[:raw_len]
    masked_tail = remaining[raw_len:]
    if masked_tail:
        if xor_key is None:
            logger.debug("V2 container needs IMAGE_XOR_KEY — not configured")
            return None
        masked_tail = bytes(byte ^ xor_key for byte in masked_tail)

    return plain_head + plain_body + masked_tail


def decrypt_dat_file(
    path,
    xor_key: Optional[int] = None,
    aes_key_text: Optional[str] = None,
) -> Optional[Tuple[bytes, str]]:
    """Decrypt *path* and return ``(image_bytes, extension)``.

    The extension comes from sniffing the decrypted bytes; a ``wxgf``
    container is returned as-is (extension ``"wxgf"``) because unwrapping it
    needs a codec.
    """
    try:
        data = path.read_bytes()
    except OSError:
        logger.debug("Cannot read %s", path, exc_info=True)
        return None

    plain = decrypt_dat(data, xor_key, aes_key_text)
    if not plain:
        return None

    ext = sniff_format(plain)
    if not ext:
        logger.debug("Decrypted %s is not a recognised image", path)
        return None
    return plain, ext


def _aes_ecb_decrypt(block: bytes, key: bytes) -> Optional[bytes]:
    """AES-128-ECB decrypt without padding removal."""
    try:
        from Crypto.Cipher import AES
    except ImportError:  # pragma: no cover - dependency is in requirements
        logger.warning(
            "pycryptodome not installed — .dat images cannot be decrypted"
        )
        return None
    try:
        return AES.new(key, AES.MODE_ECB).decrypt(block)
    except Exception:
        logger.debug("AES decryption failed", exc_info=True)
        return None


def _strip_pkcs7(data: bytes) -> Optional[bytes]:
    """Remove PKCS7 padding, refusing anything that does not validate.

    A wrong key produces random bytes; roughly one in 256 will look like
    valid padding, so the caller still has to sniff the result.
    """
    if not data:
        return None
    pad = data[-1]
    if pad <= 0 or pad > _AES_BLOCK or pad > len(data):
        return None
    if data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]
