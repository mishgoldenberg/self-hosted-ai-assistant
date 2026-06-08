"""
whisper_stt.py — Local speech-to-text via faster-whisper.

Runs on GPU (CUDA float16) with RTX 3060.
Falls back to CPU int8 if CUDA is unavailable.

Public API
──────────
  transcribe(audio_path: str) -> str
"""

import os
import logging

log = logging.getLogger(__name__)

_MODEL_SIZE = "small"   # good accuracy/speed balance; upgrade to "medium" if needed
_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model

    from faster_whisper import WhisperModel

    # ctranslate2 4.x requires CUDA 12; system has CUDA 11.8 — use CPU int8
    log.info("[whisper] loading %s on CPU (int8)…", _MODEL_SIZE)
    _model = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
    log.info("[whisper] model ready")
    return _model


def transcribe(audio_path: str) -> str:
    """
    Transcribe an audio file and return the detected text.
    Supports OGG/Opus (Telegram voice), WAV, MP3, etc.
    Returns an empty string if nothing was detected.
    """
    model = _get_model()
    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        vad_filter=True,          # skip silent segments
        vad_parameters={"min_silence_duration_ms": 500},
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    log.info("[whisper] transcribed (%s, %.1fs): %s", info.language, info.duration, text[:80])
    return text
