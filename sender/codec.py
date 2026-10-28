"""DCT video codec for Roblox EditableImage streaming.

Design goals: most picture quality per byte, while the decoder stays cheap in Luau.
  * YCbCr 4:2:0 - eyes care about brightness detail far more than colour detail,
    so colour is stored at half resolution in both directions (half the samples).
  * 8x8 DCT + JPEG quantization tables - spends bits where they are visible.
  * Inter frames code the residual against the decoder's own reconstruction
    (closed loop), so unchanged areas cost nearly nothing and still screens keep
    sharpening frame after frame.
  * Entropy coding is zstd, which Roblox decodes natively (EncodingService), so the
    Luau side never parses bits. Coefficients are laid out to suit zstd: planar by
    zigzag index, sign-folded to unsigned, low and high bytes in separate runs.
  * The inverse DCT is pure integer maths so Python and Luau reconstruct bit-for-bit
    identical frames; otherwise inter-frame drift would build up.

Payload (before zstd):  u8 quant level | low bytes of all coefficients | high bytes
Coefficient order: plane (Y, Cb, Cr) -> zigzag index k (0..63) -> block (row-major).
"""
import numpy as np
from compression import zstd

ZSTD_LEVEL = 6
ROUND_INTRA = 0.4
ROUND_INTER = 0.25
SHIFT = 24  # integer IDCT scale: basis is round(4096 * dct) applied twice

ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
]

# Standard JPEG tables (row-major).
Q_LUMA = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
]
Q_CHROMA = [
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
]

# Quantizer scale per level, in 1/16ths (integers so both sides agree exactly).
# Level 0 is near-lossless, the last level is very coarse.
SCALES16 = [round(16 * 0.05 * 1.16 ** i) for i in range(40)]


def _dct_matrix():
    c = np.zeros((8, 8))
    for k in range(8):
        a = np.sqrt(1 / 8) if k == 0 else np.sqrt(2 / 8)
        for n in range(8):
            c[k, n] = a * np.cos(np.pi * (2 * n + 1) * k / 16)
    return c


C = _dct_matrix()
M = np.round(C * 4096).astype(np.int64)


def quant_tables(level):
    s = SCALES16[level]
    luma = np.maximum(1, (np.array(Q_LUMA, np.int64) * s + 8) // 16)
    chroma = np.maximum(1, (np.array(Q_CHROMA, np.int64) * s + 8) // 16)
    return luma, chroma


def _blocks(plane):
    h, w = plane.shape
    return plane.reshape(h // 8, 8, w // 8, 8).swapaxes(1, 2).reshape(-1, 8, 8)


def _unblocks(b, h, w):
    return b.reshape(h // 8, w // 8, 8, 8).swapaxes(1, 2).reshape(h, w)


def idct_int(deq):
    """Exact integer inverse DCT of (n, 8, 8) int64 blocks.

    Done in float64 for speed: every product and partial sum is an integer below
    2^53, so the result is still exact.
    """
    mf = M.astype(np.float64)
    out = np.matmul(np.matmul(mf.T, deq.astype(np.float64)), mf).astype(np.int64)
    return (out + (1 << (SHIFT - 1))) >> SHIFT


_RGB_TO_YCC = np.array([
    [0.299, 0.587, 0.114],
    [-0.168736, -0.331264, 0.5],
    [0.5, -0.418688, -0.081312],
], np.float32)


def rgb_to_ycc(rgb):
    """uint8 (H, W, 3) -> float Y (H, W), Cb, Cr (H/2, W/2)."""
    ycc = rgb.astype(np.float32) @ _RGB_TO_YCC.T
    y = ycc[..., 0]
    cb = ycc[..., 1] + 128
    cr = ycc[..., 2] + 128
    h, w = y.shape
    cb = cb.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    cr = cr.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    return y, cb, cr


def ycc_to_rgb(y, cb, cr):
    """Integer conversion, mirrored exactly by the Luau decoder. Nearest chroma upsample."""
    cb = np.repeat(np.repeat(cb, 2, 0), 2, 1).astype(np.int64) - 128
    cr = np.repeat(np.repeat(cr, 2, 0), 2, 1).astype(np.int64) - 128
    y = y.astype(np.int64)
    # Fixed point (x65536) BT.601 full-range coefficients.
    r = y + ((91881 * cr + 32768) >> 16)
    g = y - ((22554 * cb + 46802 * cr + 32768) >> 16)
    b = y + ((116130 * cb + 32768) >> 16)
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


class Encoder:
    def __init__(self, w, h):
        assert w % 16 == 0 and h % 16 == 0, "size must be a multiple of 16"
        self.w, self.h = w, h
        self.recon = None  # decoder-side planes (uint8) after the last emitted frame
        self.zz = np.array(ZIGZAG)
        self.sizes = [(h, w), (h // 2, w // 2), (h // 2, w // 2)]
        self.counts = [(ph // 8) * (pw // 8) for ph, pw in self.sizes]
        self.last_level = {True: 20, False: 20}  # search starts here (per frame type)
        self._inv_q = {}

    def _inv_table(self, level):
        """1/q for every coefficient in payload order (plane -> zigzag k -> block)."""
        if level not in self._inv_q:
            luma, chroma = quant_tables(level)
            parts = [np.repeat(1.0 / (luma if i == 0 else chroma)[self.zz], n) for i, n in enumerate(self.counts)]
            self._inv_q[level] = np.concatenate(parts).astype(np.float32)
        return self._inv_q[level]

    def _coefs(self, planes, intra):
        """Float DCT coefficients of all planes, already in payload order, plus their magnitudes."""
        out = []
        for i, p in enumerate(planes):
            x = p - 128.0 if intra else p - self.recon[i].astype(np.float64)
            d = np.matmul(np.matmul(C, _blocks(x)), C.T).reshape(-1, 64)
            out.append(d[:, self.zz].T.reshape(-1))
        flat = np.concatenate(out).astype(np.float32)
        return np.abs(flat), np.signbit(flat)

    def _quantize(self, mag, neg, level, intra):
        # Dead-zone rounding (as in H.264): small values snap to zero, which stops
        # inter frames re-sending rounding noise on a still screen.
        offset = ROUND_INTRA if intra else ROUND_INTER
        a = np.floor(mag * self._inv_table(level) + offset)
        a = np.minimum(a, 16383).astype(np.int16)  # folded value must fit in u16
        return np.where(neg, -a, a)

    def _pack(self, v, level):
        u = ((v << 1) ^ (v >> 15)).view(np.uint16)  # sign fold: 0,-1,1,-2.. -> 0,1,2,3..
        lo_hi = u.view(np.uint8).reshape(-1, 2)  # little-endian (low, high) pairs
        raw = bytes([level]) + lo_hi[:, 0].tobytes() + lo_hi[:, 1].tobytes()
        return zstd.compress(raw, level=ZSTD_LEVEL)

    def _reconstruct(self, v, level, intra):
        luma, chroma = quant_tables(level)
        planes, at = [], 0
        for i, ((h, w), n) in enumerate(zip(self.sizes, self.counts)):
            q = v[at:at + 64 * n].astype(np.int64).reshape(64, n).T
            at += 64 * n
            table = luma if i == 0 else chroma
            deq = np.zeros_like(q)
            deq[:, self.zz] = q * table[self.zz]
            res = _unblocks(idct_int(deq.reshape(-1, 8, 8)), h, w)
            base = 128 if intra else self.recon[i].astype(np.int64)
            planes.append(np.clip(base + res, 0, 255).astype(np.uint8))
        return planes

    def encode(self, rgb, max_bytes, force_intra=False):
        """Best-quality encoding of rgb (H, W, 3 uint8) that fits in max_bytes.

        Returns (kind, payload) with kind 0 = intra, 1 = inter, or None when even the
        coarsest level does not fit (inter only; intra always returns something).
        """
        intra = force_intra or self.recon is None
        mag, neg = self._coefs(rgb_to_ycc(rgb), intra)
        cache = {}

        def fits(level):
            if level not in cache:
                v = self._quantize(mag, neg, level, intra)
                cache[level] = (v, self._pack(v, level))
            return len(cache[level][1]) <= max_bytes

        # Find the finest level that fits. Sizes fall as the level rises, and the
        # answer is usually near last frame's, so gallop out from there, then bisect.
        # Invariant afterwards: lo fails (or is -1), hi fits; the answer is hi.
        top = len(SCALES16) - 1
        guess = self.last_level[intra]
        step = 1
        if fits(guess):
            lo, hi = -1, guess
            while hi - step >= 0:
                if not fits(hi - step):
                    lo = hi - step
                    break
                hi, step = hi - step, step * 2
        elif not fits(top):
            if not intra:
                return None
            lo, hi = top - 1, top  # keyframes always go out, at the coarsest level
        else:
            lo, hi = guess, top
            while lo + step < top:
                if fits(lo + step):
                    hi = lo + step
                    break
                lo, step = lo + step, step * 2
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if fits(mid):
                hi = mid
            else:
                lo = mid
        v, payload = cache[hi]
        self.last_level[intra] = hi
        self.recon = self._reconstruct(v, hi, intra)
        return (0 if intra else 1), payload

    def picture(self):
        return ycc_to_rgb(*self.recon)


class Decoder:
    """Reference decoder: what the Luau side must reproduce exactly."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.planes = None
        self.zz = np.array(ZIGZAG)

    def decode(self, kind, payload):
        raw = np.frombuffer(zstd.decompress(payload), np.uint8)
        level = int(raw[0])
        n = (len(raw) - 1) // 2
        u = raw[1:1 + n].astype(np.int64) | (raw[1 + n:].astype(np.int64) << 8)
        v = (u >> 1) ^ -(u & 1)
        luma, chroma = quant_tables(level)
        sizes = [(self.h, self.w), (self.h // 2, self.w // 2), (self.h // 2, self.w // 2)]
        at = 0
        planes = []
        for i, (h, w) in enumerate(sizes):
            nb = (h // 8) * (w // 8)
            q = v[at:at + nb * 64].reshape(64, nb).T
            at += nb * 64
            table = luma if i == 0 else chroma
            deq = np.zeros_like(q)
            deq[:, self.zz] = q * table[self.zz]
            res = _unblocks(idct_int(deq.reshape(-1, 8, 8)), h, w)
            base = 128 if kind == 0 else self.planes[i].astype(np.int64)
            planes.append(np.clip(base + res, 0, 255).astype(np.uint8))
        self.planes = planes
        return ycc_to_rgb(*planes)
