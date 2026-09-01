import os
import sys
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from codexrelay.version import __version__


def optional_update_plist() -> dict[str, object]:
    """Return Sparkle metadata only for an explicitly configured release build."""

    feed_url = os.environ.get("CODEXRELAY_SPARKLE_FEED_URL", "").strip()
    public_key = os.environ.get("CODEXRELAY_SPARKLE_PUBLIC_ED_KEY", "").strip()
    automatic_checks = os.environ.get("CODEXRELAY_UPDATE_CHECKS_AUTOMATICALLY")
    metadata: dict[str, object] = {}
    if feed_url:
        metadata["SUFeedURL"] = feed_url
    if public_key:
        metadata["SUPublicEDKey"] = public_key
    if automatic_checks is not None:
        metadata["SUEnableAutomaticChecks"] = automatic_checks.lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return metadata

analysis = Analysis(
    [str(SRC / "codexrelay" / "ui" / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "mypy",
        "openai_codex_cli_bin",
        "pydantic.mypy",
        "pydantic.v1.mypy",
    ],
    noarchive=False,
    optimize=1,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="CodexRelay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=os.environ.get("CODEXRELAY_TARGET_ARCH", "arm64"),
)

collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="CodexRelay",
)

application = BUNDLE(
    collected,
    name="CodexRelay.app",
    icon=str(ROOT / "assets" / "CodexRelay.icns"),
    bundle_identifier="com.cwwen.codexrelay",
    info_plist={
        "CFBundleDisplayName": "CodexRelay",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Personal build",
        "CodexRelayBuildTime": os.environ.get("CODEXRELAY_BUILD_TIME", "Packaged build"),
        **optional_update_plist(),
    },
)
