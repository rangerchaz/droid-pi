"""Pure utility helpers: PCM RMS, motion detection."""
import math
import struct

import cv2

from . import state
from .config import MOTION_THRESHOLD, MOTION_PIXEL_PCT


def compute_rms(pcm_data):
    """Compute RMS energy of 16-bit PCM audio."""
    if len(pcm_data) < 2:
        return 0
    count = len(pcm_data) // 2
    fmt = f'<{count}h'
    try:
        samples = struct.unpack(fmt, pcm_data[:count * 2])
    except struct.error:
        return 0
    if not samples:
        return 0
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / count)


def detect_motion(frame, threshold=MOTION_THRESHOLD, pct=MOTION_PIXEL_PCT):
    """Compare current frame to previous, return True if motion detected.
    Stores the last grayscale frame on state.prev_frame_gray.
    """
    small = cv2.resize(frame, (160, 120))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    if state.prev_frame_gray is None:
        state.prev_frame_gray = gray
        return False

    diff = cv2.absdiff(gray, state.prev_frame_gray)
    state.prev_frame_gray = gray

    changed = (diff > threshold).sum()
    total = 160 * 120
    percent = (changed / total) * 100

    return percent >= pct
