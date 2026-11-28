# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("playwright")
datas += [
    ("adofai_publisher/assets/icons/app-logo.png", "adofai_publisher/assets/icons"),
    ("adofai_publisher/assets/icons/app-icon.ico", "adofai_publisher/assets/icons"),
    (
        "adofai_publisher/assets/icons/difficulty",
        "adofai_publisher/assets/icons/difficulty",
    ),
    ("bridge_mod/Info.json", "bridge_mod"),
    ("bridge_mod/ADOFAIGOBridge.dll", "bridge_mod"),
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)


def _drop_tool_runtime_payload(entries):
    """避免把本地工具链缓存里的 DLL 误当成应用依赖。"""
    markers = (
        "\\.cache\\codex-runtimes\\",
        "\\poppler\\",
        "\\libheif\\",
        "\\jxrlib\\",
    )
    kept = []
    dropped = []
    for entry in entries:
        source = str(entry[1]).replace("/", "\\").lower()
        if any(marker in source for marker in markers):
            dropped.append(entry[0])
        else:
            kept.append(entry)
    if dropped:
        print("Dropped tool-runtime payload:", ", ".join(sorted(set(dropped))))
    return kept


a.binaries = _drop_tool_runtime_payload(a.binaries)
a.datas = _drop_tool_runtime_payload(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ADOFAI GO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    icon="adofai_publisher/assets/icons/app-icon.ico",
    version=os.environ.get("ADOFAI_BUILD_VERSION_FILE") or None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
