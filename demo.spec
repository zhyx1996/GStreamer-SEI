# -*- mode: python ; coding: utf-8 -*-

import gstreamer_libs
gstreamer_libs.setup_python_environment()

from PyInstaller.utils.hooks import collect_all


block_cipher = None

datas = []
binaries = []
hiddenimports = [
    "gi",
    "gi._error",
    "gi._option",
    "gi.repository.Gst",
    "gi.repository.GstApp",
    "gi.repository.GstRtspServer",
    "gi.repository.GLib",
    "gi.repository.GObject",
    "gi.repository.Gio",
]

for package in [
    "gstreamer_libs",
    "gstreamer_plugins",
    "gstreamer_plugins_libs",
    "gstreamer_plugins_restricted",
    "gstreamer_plugins_gpl",
    "gstreamer_plugins_gpl_restricted",
    "gstreamer_python",
    "gstreamer_ext_runtime",
]:
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    ["demo.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="demo_gst",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
