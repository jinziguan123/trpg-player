# PyInstaller 打包桌面版后端为单个 sidecar 二进制（供 Tauri 外壳启动）。
# 用法：cd server && .venv/bin/pyinstaller desktop.spec --noconfirm
#
# 打包内容：run_desktop.py 入口 + 整个 app 包；数据文件带上
#   - alembic/     → 迁移脚本（启动时 run_migrations 从 script_location 读取）
#   - web_dist/    → 前端构建产物（main.py frozen 时从 sys._MEIPASS/web_dist 同源托管）
# 不打包 fastembed/onnxruntime（规则书 RAG 的嵌入运行时才用、且模型首次运行下载）；
# 缺它时嵌入模块已做优雅降级（未装不影响其余功能），RAG 的完整打包留作后续。
import os

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = os.path.abspath(os.getcwd())
WEB_DIST = os.path.abspath(os.path.join(SPEC_DIR, "..", "apps", "web", "dist"))

datas = [
    (os.path.join(SPEC_DIR, "alembic"), "alembic"),
]
if os.path.isdir(WEB_DIST):
    datas.append((WEB_DIST, "web_dist"))

hiddenimports = [
    "app.main",
    # uvicorn 的动态加载子模块（PyInstaller 静态分析抓不全）
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
] + collect_submodules("alembic")

a = Analysis(
    ["run_desktop.py"],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["fastembed", "onnxruntime", "tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="trpg-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
