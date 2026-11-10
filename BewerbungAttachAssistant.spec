# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build specification for Bewerbung Attach Assistant.

Build a single Windows executable (run in the project folder, inside the
activated virtual environment):

    pyinstaller --noconfirm --clean BewerbungAttachAssistant.spec

Result: dist\\BewerbungAttachAssistant.exe

Notes
-----
* google-api-python-client ships ~600 API discovery documents (~110 MB).
  Only the Gmail document is needed, so all others are removed below.
* The OAuth client file, token, settings and history are NOT bundled; they
  live in %LOCALAPPDATA%\\BewerbungAttachAssistant on the user's machine.
"""

import os

import googleapiclient

GMAIL_DISCOVERY_DOC = os.path.join(
    os.path.dirname(googleapiclient.__file__), "discovery_cache", "documents", "gmail.v1.json"
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[(GMAIL_DISCOVERY_DOC, os.path.join("googleapiclient", "discovery_cache", "documents"))],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "pyinstaller", "IPython", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)


def _keep(entry):
    """Drop every bundled discovery document except Gmail's."""
    dest = entry[0].replace("\\", "/")
    if "googleapiclient/discovery_cache/documents/" in dest:
        return dest.endswith("/gmail.v1.json")
    return True


a.datas = [entry for entry in a.datas if _keep(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BewerbungAttachAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # windowed application (no console window)
    disable_windowed_traceback=False,
    icon=None,
)
