# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建描述 —— 单文件、带控制台。

用法（在**仓库根目录**跑）：

    pip install -e ".[build]"
    pyinstaller packaging/cubemx-cli.spec --noconfirm

产物是 ``dist/cubemx-cli.exe``，一个文件，拷到哪儿都能跑。

这个 spec 之所以短，是因为包里本来就没有需要照顾的东西：

* **没有数据文件** —— ``src/`` 下除了 ``.py`` 一个文件都没有，所以 ``datas=[]``。
* **没有第三方依赖** —— ``dependencies = []`` 是刻意保持的，所以 ``hiddenimports=[]``。
  哪天加了依赖，这里多半也要跟着长。
* **不看 ``__file__`` / ``sys.frozen`` / ``os.getcwd``** —— 所以不需要担心单文件模式
  解包到临时目录之后路径对不上。

``upx=False`` 是故意的：UPX 压出来的 exe 被各家杀软误报的概率高得离谱，而这个东西
是要拷给别人用的。省几 MB 不值当。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cubemx-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 必须：它的输出就是接口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
