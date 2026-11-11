"""
alert_sound.py - Attention sound for "Sicherheitsabfrage needed".

Generated in memory (a clear two-tone signal repeated twice), so it is heard
even when the Windows sound scheme is set to "No sounds".  Playback runs on a
background thread.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import threading
import wave

_CACHE: dict[str, bytes] = {}


def _tone_wav(notes: list[tuple[float, float]], sample_rate: int = 22050) -> bytes:
    frames = bytearray()
    for freq, length in notes:
        count = int(sample_rate * length)
        for i in range(count):
            t = i / sample_rate
            if freq <= 0:
                value = 0.0
            else:
                envelope = min(1.0, i / (0.008 * sample_rate)) * min(1.0, (count - i) / (0.02 * sample_rate))
                value = 0.4 * envelope * math.sin(2 * math.pi * freq * t)
            frames += struct.pack("<h", int(value * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def attention_wav() -> bytes:
    """Two-tone signal (high-low, twice): 'please come here'."""
    return _tone_wav([(988, 0.18), (740, 0.18), (0, 0.12), (988, 0.18), (740, 0.3)])


def done_wav() -> bytes:
    """Short rising chime: 'finished'."""
    return _tone_wav([(784, 0.12), (1047, 0.25)])


def _play(name: str, factory, fallback=None) -> None:
    if sys.platform != "win32":
        if fallback:
            fallback()
        return
    try:
        import winsound
        data = _CACHE.setdefault(name, factory())
        threading.Thread(target=lambda: winsound.PlaySound(data, winsound.SND_MEMORY),
                         daemon=True).start()
    except Exception:
        if fallback:
            fallback()


def play_attention(fallback=None) -> None:
    _play("attention", attention_wav, fallback)


def play_done(fallback=None) -> None:
    _play("done", done_wav, fallback)
