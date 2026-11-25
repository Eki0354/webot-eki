# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for webot Desktop.

Build: pyinstaller build.spec
Output: dist/webot.exe
"""
import sys
import site
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH)

# ── Resolve webview runtime DLLs dynamically ───────────────────────────
def _find_webview_runtime_dir():
    """Find the webview package's runtime directory in site-packages."""
    for sp in site.getsitepackages():
        candidate = Path(sp) / "webview" / "lib"
        if candidate.exists():
            return candidate
    # Fallback: try user site-packages
    user_sp = site.getusersitepackages()
    if user_sp:
        candidate = Path(user_sp) / "webview" / "lib"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "webview package not found. Install with: pip install pywebview"
    )

_webview_dir = _find_webview_runtime_dir()
_webview_runtime = _webview_dir / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
_webview_interop = _webview_dir / "WebBrowserInterop.x64.dll"


def _find_certifi_bundle():
    """Return certifi's cacert.pem as a PyInstaller datas entry, or [] .

    The frozen EXE must be able to verify TLS by itself: this machine's
    CPython reports no `cafile`/`capath`, and link fetching then fails on
    sites whose chain needs an intermediate (example.com fails, python.org
    passes).  Optional — builds without certifi still work, only less
    reliably, so a missing package must not break the build.
    """
    for sp in (site.getsitepackages() + [site.getusersitepackages()]):
        candidate = Path(sp) / "certifi" / "cacert.pem"
        if candidate.exists():
            return [(str(candidate), "certifi")]
    return []


def _whisper_bundle():
    """(datas, binaries) needed by local Whisper, or ([], []) when absent.

    Transcribing a clip's speech (``MEDIA_VIDEO_ASR_ENABLED``) runs
    faster-whisper, which needs three things PyInstaller does not find on its
    own:

    * ``ctranslate2``'s DLLs — the inference engine, and by far the largest
      entry (58 MB).  There is no contributed hook for it, and its libraries
      sit beside the package rather than being picked up by the static scan.
    * the Silero VAD model that ships inside ``faster_whisper/assets``; the
      fast decode path filters silence with it, and without the file that
      call raises instead of degrading.
    * ``opencc``'s conversion tables — traditional → simplified, which every
      transcript goes through.

    ``av`` and ``onnxruntime`` need nothing here: their contributed hooks
    already collect the FFmpeg and provider libraries.

    Optional on purpose.  A machine without these packages must still build a
    working EXE — it then describes a clip from its picture alone, the same
    trade-off as a missing ``ffmpeg.exe``.
    """
    try:
        from PyInstaller.utils.hooks import (
            collect_data_files,
            collect_dynamic_libs,
        )
    except Exception:
        return [], []

    datas: list = []
    for package in ("faster_whisper", "opencc"):
        try:
            datas += collect_data_files(package)
        except Exception:
            pass

    binaries: list = []
    try:
        binaries += collect_dynamic_libs("ctranslate2")
    except Exception:
        pass
    return datas, binaries


_whisper_datas, _whisper_binaries = _whisper_bundle()


if not _webview_runtime.exists():
    raise FileNotFoundError(f"WebView2Loader.dll not found at {_webview_runtime}")
if not _webview_interop.exists():
    raise FileNotFoundError(f"WebBrowserInterop.x64.dll not found at {_webview_interop}")

a = Analysis(
    ['desktop.py'],
    pathex=[str(PROJECT_ROOT)],
    binaries=[
        (str(_webview_runtime), './runtimes/win-x64/native'),
        (str(_webview_interop), './lib'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'wcdb_api.dll'), 'native/windows'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'WCDB.dll'), 'native/windows'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'MSVCP140.dll'), 'native/windows'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'VCRUNTIME140.dll'), 'native/windows'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'VCRUNTIME140_1.dll'), 'native/windows'),
        (str(PROJECT_ROOT / 'native' / 'windows' / 'wx_key.dll'), 'native/windows'),
        # NOTE: native/windows/ffmpeg.exe is deliberately NOT listed here.
        # It is a 121 MB binary and embedding it would grow the EXE from ~56 MB
        # to ~180 MB for a feature (shared-video frame sampling) that the
        # reply path only touches when someone asks about a video link.
        # src/media/video.py instead looks for it *beside* the EXE
        # (dist/native/windows/ffmpeg.exe); copy it there after packing:
        #   cp native/windows/ffmpeg.exe dist/native/windows/
        # Without it the bot degrades to the text-only answer, which is the
        # same trade-off as voice recognition.
    ]
    # ctranslate2's DLLs (58 MB) — the local Whisper engine.  Empty when the
    # package is not installed, in which case the build still works and video
    # transcription simply degrades to describing the picture alone.
    + _whisper_binaries,
    datas=[
        ('ui/dist', 'ui/dist'),
        ('.env.example', '.'),
        # data/ is runtime-generated — do NOT bundle into read-only _MEIPASS
    ]
    # certifi's CA bundle.  The frozen EXE otherwise inherits the host's
    # `ssl` defaults, which on Windows Python ship no cafile/capath at all —
    # link fetching then fails on sites whose chain needs an intermediate
    # (measured: example.com fails, python.org passes).  Optional: without
    # certifi installed the build still works, just less reliably.
    + _find_certifi_bundle()
    # Silero VAD model + opencc conversion tables: the fast decode path
    # filters silence with the first, and every transcript is converted from
    # traditional to simplified with the second.  Both are data files inside
    # their packages, so the static import scan never sees them.
    + _whisper_datas,
    hiddenimports=[
        'src', 'src.bot', 'src.config', 'src.main',
        'src.db', 'src.db.schema', 'src.db.store',
        'src.trigger', 'src.trigger.detector',
        'src.summarize', 'src.summarize.base', 'src.summarize.claude_backend',
        'src.summarize.deepseek_backend', 'src.summarize.models', 'src.summarize.prompts',
        'src.proactive', 'src.proactive.gate', 'src.proactive.modes',
        'src.proactive.rate_tracker', 'src.proactive.sticky',
        'src.memory', 'src.memory.consolidator',
        # Imported lazily by MessageRouter (rolling digest of the chat that
        # scrolled out of the context window), so the static scan misses it.
        'src.memory.digest',
        'src.integrations', 'src.integrations.feishu',
        'src.integrations.feishu.client', 'src.integrations.feishu.exporter',
        'src.integrations.feishu.knowledge',
        'src.wechat', 'src.wechat.base', 'src.wechat.wcdb_backend',
        'src.wechat.wcdb_client', 'src.wechat.mac_hybrid_backend',
        'src.wechat.mac_ui_backend', 'src.wechat.window_controller',
        'src.wechat.keyboard', 'src.wechat.helpers', 'src.wechat.extract_key',
        'src.wechat.native', 'src.wechat.native.injector',
        'src.media', 'src.media.appmsg', 'src.media.blob',
        'src.media.emoji', 'src.media.pipeline', 'src.media.vision',
        'src.media.backfill',
        'src.media.dat_decrypt', 'src.media.wxgf',
        'src.media.image', 'src.media.keys', 'src.media.link_fetcher',
        # Frame sampling.  Imported lazily inside MediaPipeline.describe_video
        # and link_fetcher._video_urls, so the static scan cannot see it;
        # without this line the frozen EXE silently answers "看不到视频".
        'src.media.video',
        # Video speech.  MediaPipeline._get_asr imports src.voice.asr inside a
        # method, so the static scan misses it and the frozen EXE would fall
        # back to describing every clip from its picture alone.  The whole
        # voice package comes along — the ASR factory is reached through
        # ``src.voice``'s package init.
        'src.voice', 'src.voice.asr',
        'faster_whisper', 'faster_whisper.transcribe', 'faster_whisper.vad',
        'faster_whisper.audio', 'faster_whisper.feature_extractor',
        'faster_whisper.tokenizer', 'faster_whisper.utils',
        'ctranslate2', 'onnxruntime', 'av', 'opencc', 'numpy',
        # Headless render fallback for pages whose player/body is script-built.
        # Imported lazily inside link_fetcher._render_page, so the static scan
        # misses it; without this line the frozen EXE silently never renders
        # and B站/YouTube-style video pages stay unreadable.
        'src.media.browser_render',
        'zstandard',
        # AES-128-ECB for the .dat image container.  PyAV (HEVC) is
        # deliberately excluded below: wxgf images then degrade to the
        # [图片] placeholder instead of growing the EXE by ~100 MB.
        'Crypto', 'Crypto.Cipher', 'Crypto.Cipher.AES',
        # Chain repair in link_fetcher imports these inside functions, so
        # PyInstaller's static scan is the only thing that can find them.
        # Without them the frozen EXE silently loses TLS repair and falls
        # back to plaintext (or fails outright) on sites that send the leaf
        # certificate alone.
        'cryptography', 'cryptography.x509',
        'cryptography.x509.verification',
        'cryptography.hazmat.primitives.serialization',
        'certifi',
        'src.web', 'src.web.server',
        'src.router', 'src.welcome',
        'src.todo', 'src.todo.store', 'src.todo.handler',
        'src.nickname', 'src.admin', 'src.fun',
        'src.utils', 'src.utils.logging_config',
        # Hides the console window of every ffmpeg child.  Imported by
        # src.media.video (lazy) and src.voice.decoder.
        'src.utils.subprocess_win',
        'dotenv', 'anthropic', 'openai', 'pydantic',
        'uiautomation',
        'webview', 'webview.platforms', 'webview.platforms.edgechromium',
        'PIL', 'PIL.Image', 'PIL.ImageDraw',
        'psutil', 'pyperclip',
        'win32api', 'comtypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'jedi', 'IPython',
              # Local Whisper for video speech (MEDIA_VIDEO_ASR_ENABLED) needs
              # faster_whisper and its engine, so these four are no longer
              # excluded: ctranslate2 ships the 58 MB inference DLL, numpy is
              # its array type, av decodes the WAV, and onnxruntime runs the
              # Silero VAD that drops silence.  ``pysilk`` stays out — it
              # decodes WeChat's SILK voice messages, which this feature
              # never touches, and voice recognition is not enabled by it.
              'pysilk', 'sounddevice', 'soundfile',
              # Exclude packages that are NOT webot dependencies but may
              # be installed in the local Python environment.  Bundling
              # them bloats the EXE by hundreds of MB.
              'torch', 'torchvision', 'transformers', 'pandas',
              'cv2', 'sklearn', 'ultralytics', 'joblib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='webot',
    icon='image/logo_assets/logo.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
