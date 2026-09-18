# -*- mode: python ; coding: utf-8 -*-

import sys
import os

# ---------- 版本信息 ----------
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo,
    StringTable, StringStruct, VarFileInfo, VarStruct,
)

# ---------- 收集 pywebview(webview) 全部依赖（DLL/静态资源）----------
# 关键修复：旧 spec 只声明 hiddenimports，未把 webview 包的 platforms/lib
# 下 WebView2Loader.dll、Microsoft.Web.WebView2.*.dll 等二进制资源收进 exe，
# 导致打包后 WebView2 后端初始化失败 → 黑屏。
try:
    from PyInstaller.utils.hooks import collect_all
    _wb2_datas, _wb2_binaries, _wb2_hidden = collect_all('webview')
    print(f"[spec] collect_all('webview'): datas={len(_wb2_datas)} binaries={len(_wb2_binaries)} hidden={len(_wb2_hidden)}")
except Exception as _ce:
    print(f"[spec] collect_all('webview') 失败: {_ce}")
    _wb2_datas, _wb2_binaries, _wb2_hidden = [], [], []

_version_file = os.path.join(os.getcwd(), 'exe_version.py')
try:
    with open(_version_file, 'r', encoding='utf-8') as _f:
        _code = compile(_f.read(), _version_file, 'exec')
    _ns = {
        'VSVersionInfo': VSVersionInfo,
        'FixedFileInfo': FixedFileInfo,
        'StringFileInfo': StringFileInfo,
        'StringTable': StringTable,
        'StringStruct': StringStruct,
        'VarFileInfo': VarFileInfo,
        'VarStruct': VarStruct,
    }
    exec(_code, _ns)
    version_info = _ns.get('version_info')
except Exception as _ve:
    print(f"Version info load failed: {_ve}")
    version_info = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_wb2_binaries,
    datas=_wb2_datas + [('hotkeys.json', '.')],
    hiddenimports=['webview', 'pynput', 'tkinter.commondialog', 'tkinter.messagebox', 'tkinter.ttk', 'email', 'email.mime', 'email.mime.text', 'email.mime.multipart', 'http.server', 'http.client', 'socketserver', 'concurrent.futures', 'asyncio'] + _wb2_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
            'tkinter.test', 'test', 'unittest', 'pydoc',
            'distutils', 'setuptools', 'wheel', 'pip',
            'numpy', 'matplotlib', 'PIL', 'pandas',
            'turtle',
        ],
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
    name='跟跑助手',
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
    icon='icon.ico',
    version=version_info,
)