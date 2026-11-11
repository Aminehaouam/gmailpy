"""
sound.py - Short "success" chime played after an e-mail was sent.

The chime is generated in memory (a rising three-note arpeggio), so it is
heard even when the Windows sound scheme is set to "No sounds".  Playback
runs on a background thread and never blocks the application.
"""

from __future__ import annotations

import io
import logging
import math
import struct
import sys
import threading
import wave

log = logging.getLogger(__name__)

_CHIME: bytes | None = None


def success_chime_wav(sample_rate: int = 22050) -> bytes:
    """WAV bytes of a ~0.6 s rising chime (C6 - E6 - G6)."""
    notes = [(1046.5, 0.13), (1318.5, 0.13), (1568.0, 0.34)]
    frames = bytearray()
    for freq, length in notes:
        count = int(sample_rate * length)
        for i in range(count):
            t = i / sample_rate
            envelope = min(1.0, i / (0.01 * sample_rate)) * math.exp(-4.0 * t / length)
            value = 0.35 * envelope * (math.sin(2 * math.pi * freq * t)
                                       + 0.3 * math.sin(4 * math.pi * freq * t))
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def play_success_sound(fallback=None) -> None:
    """Play the chime (Windows).  On other systems call *fallback* (e.g. the
    Tk bell) if given."""
    global _CHIME
    if sys.platform != "win32":
        if fallback:
            fallback()
        return
    try:
        import winsound
        if _CHIME is None:
            _CHIME = success_chime_wav()
        data = _CHIME
        threading.Thread(target=lambda: winsound.PlaySound(data, winsound.SND_MEMORY),
                         daemon=True, name="success-sound").start()
    except Exception as exc:  # sound must never break anything
        log.debug("Could not play sound: %s", exc)
        if fallback:
            fallback()
