"""ASR (Automatic Speech Recognition) — two backends.

- LocalWhisperASR:  faster-whisper, free, offline, ~1 GB RAM (default)
- OpenAiWhisperASR: OpenAI Whisper API, $0.006/min, zero local memory
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencc import OpenCC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TranscribeResult:
    text: str            # Recognised text
    confidence: float    # 0.0 ~ 1.0
    duration_sec: float  # Audio length in seconds


class ASRError(Exception):
    """Raised when transcription fails."""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AbstractASR(ABC):
    """Voice → text interface."""

    @abstractmethod
    def transcribe(self, audio_path: Path,
                   language: str = "zh") -> TranscribeResult:
        """Transcribe an audio file (.wav) to text."""
        ...

    def transcribe_media(self, audio_path: Path,
                         language: str = "zh") -> TranscribeResult:
        """Transcribe a video's audio track, where speed matters more.

        Separate from :meth:`transcribe` because the two have different
        contracts.  A voice message is short and its exact wording may matter,
        so it gets the full decoder.  A video's sound is a minute or two of
        background speech whose only job is to explain the picture, and it
        runs while someone waits for a reply — so the fast path wins even when
        it costs a little accuracy.

        The default is plain :meth:`transcribe`: a cloud backend has no local
        decoder knobs to turn, and no long wait to shorten.
        """
        return self.transcribe(audio_path, language)


# ---------------------------------------------------------------------------
# Local Whisper (faster-whisper)
# ---------------------------------------------------------------------------

class LocalWhisperASR(AbstractASR):
    """Local Whisper model via faster-whisper.

    The model is downloaded on first use to ``data/models/`` and kept in
    memory thereafter, so subsequent calls are fast and offline.
    """

    def __init__(self, model_size: str = "small",
                 model_dir: str = "data/models",
                 to_simplified: bool = True,
                 cpu_threads: int = 0) -> None:
        """
        Args:
            model_size: tiny / base / small / medium (small ≈ 1 GB RAM).
            model_dir:  Directory to cache downloaded models.
            to_simplified: Convert traditional Chinese → simplified via opencc.
            cpu_threads: Decoder threads; 0 picks automatically.  See
                :meth:`_resolve_threads` for why more is not better.
        """
        self._model_size = model_size
        self._model_dir = model_dir
        self._to_simplified = to_simplified
        self._cpu_threads = cpu_threads
        self._opencc: Optional[OpenCC] = None
        self._model = None  # lazy-init

    @staticmethod
    def _resolve_threads(requested: int = 0) -> int:
        """Threads to hand CTranslate2; 0 means "decide for me".

        Half the logical cores (≈ physical cores), clamped to 2-8.  Measured
        by a sibling project on a 6-core/12-thread CPU with the ``small``
        model and 4.2 s of audio: 12 threads took 38.80 s, 4 threads 6.53 s,
        6 threads 5.21 s.  Hyperthreads fighting over one memory bus made it
        six times slower, so the logical count is the wrong number to give.
        """
        if requested > 0:
            return max(1, min(64, requested))
        try:
            logical = os.cpu_count() or 4
        except Exception:
            logical = 4
        return max(2, min(8, logical // 2))

    # -- Public API --------------------------------------------------------

    def transcribe(self, audio_path: Path,
                   language: str = "zh") -> TranscribeResult:
        """Transcribe a voice message: full decoder, best accuracy."""
        return self._run(audio_path, language, fast=False)

    def transcribe_media(self, audio_path: Path,
                         language: str = "zh") -> TranscribeResult:
        """Transcribe a video's sound: same model, cheapest decode.

        Three changes from the voice-message path, all aimed at a reply
        someone is waiting for:

        * ``beam_size=1`` (greedy) instead of 5 — one hypothesis instead of
          five, roughly halving decode time.
        * ``without_timestamps=True`` — only the words are wanted, and
          timestamp decoding is a visible share of the work.
        * ``vad_filter=True`` — a clip's first seconds are often music or
          silence, and the voice-activity filter drops those rather than
          decoding them into hallucinated text.

        ``condition_on_previous_text=False`` goes with the VAD: on a long
        no-speech stretch Whisper will loop a phrase it already emitted, and
        with conditioning on that loops for the rest of the file.
        """
        return self._run(audio_path, language, fast=True)

    def _run(self, audio_path: Path, language: str,
             *, fast: bool) -> TranscribeResult:
        """Shared decode path; *fast* swaps in the video-oriented settings."""
        model = self._get_model()

        options: dict = {"language": language}
        if fast:
            options.update(
                beam_size=1,
                vad_filter=True,
                without_timestamps=True,
                condition_on_previous_text=False,
            )
        else:
            options.update(beam_size=5, vad_filter=False)

        try:
            segments, info = model.transcribe(str(audio_path), **options)
        except Exception as exc:
            raise ASRError(f"LocalWhisper transcribe failed: {exc}") from exc

        # Collect text from all segments
        text_parts = []
        total_confidence = 0.0
        seg_count = 0
        for seg in segments:
            if seg.text:
                text_parts.append(seg.text.strip())
            total_confidence += getattr(seg, "avg_logprob", -1.0)
            seg_count += 1

        text = " ".join(text_parts)
        if self._to_simplified:
            text = self._get_opencc().convert(text)
        # Convert avg_logprob → confidence estimate (heuristic)
        if seg_count > 0:
            avg_logprob = total_confidence / seg_count
            # Map logprob [-2, 0] roughly to [0, 1]
            confidence = min(1.0, max(0.0, (avg_logprob + 2.0) / 2.0))
        else:
            confidence = 0.0

        logger.info(
            "LocalWhisper%s: %.1fs audio → %d chars (confidence=%.2f)",
            " (fast)" if fast else "", info.duration, len(text), confidence,
        )
        return TranscribeResult(
            text=text,
            confidence=confidence,
            duration_sec=info.duration,
        )

    # -- Internal ----------------------------------------------------------

    def _get_model(self):
        if self._model is not None:
            return self._model

        from faster_whisper import WhisperModel

        threads = self._resolve_threads(self._cpu_threads)
        logger.info(
            "Loading local Whisper model '%s' (first use downloads ~500 MB, "
            "%d threads) ...",
            self._model_size, threads,
        )
        # Compute type: auto-select best for current hardware
        self._model = WhisperModel(
            self._model_size,
            device="cpu",
            compute_type="int8",
            download_root=self._model_dir,
            cpu_threads=threads,
        )
        logger.info("LocalWhisper model loaded (size=%s)", self._model_size)
        return self._model

    def _get_opencc(self) -> OpenCC:
        if self._opencc is None:
            self._opencc = OpenCC("t2s")
        return self._opencc


# ---------------------------------------------------------------------------
# OpenAI Whisper API
# ---------------------------------------------------------------------------

class OpenAiWhisperASR(AbstractASR):
    """OpenAI Whisper API — cloud-based transcription.

    Uses the same ``openai`` client the project already depends on.
    Cost: $0.006 / minute (no free tier).
    """

    def __init__(self, api_key: str = "",
                 base_url: str = "",
                 to_simplified: bool = True) -> None:
        """
        Args:
            api_key:  OpenAI API key (falls back to OPENAI_API_KEY env var).
            base_url: Custom API endpoint (e.g. proxy).
            to_simplified: Convert traditional Chinese → simplified via opencc.
        """
        self._api_key = api_key
        self._base_url = base_url
        self._to_simplified = to_simplified
        self._opencc: Optional[OpenCC] = None
        self._client = None  # lazy-init

    # -- Public API --------------------------------------------------------

    def transcribe(self, audio_path: Path,
                   language: str = "zh") -> TranscribeResult:
        client = self._get_client()

        try:
            with open(audio_path, "rb") as fh:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=fh,
                    language=language,
                    response_format="verbose_json",
                )
        except Exception as exc:
            raise ASRError(f"OpenAI Whisper API call failed: {exc}") from exc

        text = result.text.strip() if result.text else ""
        if self._to_simplified and text:
            text = self._get_opencc().convert(text)
        # Whisper API returns logprob-based segments; extract avg confidence
        confidence = 0.8  # default when no detailed info
        duration = 0.0
        if hasattr(result, "segments") and result.segments:
            logprobs = []
            for seg in result.segments:
                if hasattr(seg, "avg_logprob") and seg.avg_logprob is not None:
                    logprobs.append(seg.avg_logprob)
            if logprobs:
                avg = sum(logprobs) / len(logprobs)
                confidence = min(1.0, max(0.0, (avg + 2.0) / 2.0))
        if hasattr(result, "duration") and result.duration:
            duration = float(result.duration)

        logger.info(
            "OpenAIWhisper: %.1fs audio → %d chars (confidence=%.2f)",
            duration, len(text), confidence,
        )
        return TranscribeResult(
            text=text,
            confidence=float(confidence),
            duration_sec=duration,
        )

    # -- Internal ----------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client

        import os as _os
        from openai import OpenAI

        kwargs = {}
        api_key = self._api_key or _os.getenv("OPENAI_API_KEY", "")
        if api_key:
            kwargs["api_key"] = api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url

        self._client = OpenAI(**kwargs)
        return self._client

    def _get_opencc(self) -> OpenCC:
        if self._opencc is None:
            self._opencc = OpenCC("t2s")
        return self._opencc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_asr(config, model: str = "") -> AbstractASR:
    """Build the ASR backend specified in *config*.

    Args:
        config: A BotConfig instance (or any object with the voice_* fields).
        model: Local model size to use instead of ``voice_local_model``.
            A clip's sound only has to be understood roughly, so the video
            path can pick a smaller model than voice messages do without
            dragging voice-message accuracy down with it.  Ignored by backends
            that take no local model.

    Returns:
        An AbstractASR implementation.
    """
    backend = getattr(config, "voice_asr_backend", "local_whisper")

    if backend == "openai_whisper":
        logger.info("ASR backend: OpenAI Whisper API (cloud)")
        return OpenAiWhisperASR(
            api_key=getattr(config, "voice_openai_api_key", ""),
            base_url=getattr(config, "voice_openai_base_url", ""),
            to_simplified=getattr(config, "voice_asr_to_simplified", True),
        )
    else:
        size = model or getattr(config, "voice_local_model", "small")
        logger.info("ASR backend: Local Whisper (faster-whisper, offline, %s)", size)
        return LocalWhisperASR(
            model_size=size,
            to_simplified=getattr(config, "voice_asr_to_simplified", True),
        )
