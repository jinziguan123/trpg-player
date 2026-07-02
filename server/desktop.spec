# PyInstaller 打包桌面版后端（onedir：启动不每次解压，比 onefile 快很多）。
# 用法：cd server && .venv/bin/pyinstaller desktop.spec --noconfirm
# 产物：dist/trpg-server/（含 trpg-server 可执行 + _internal 依赖），由 Tauri 作为 resources
# 打进 .app，运行时 spawn 其中的可执行。
#
# 打包内容：run_desktop.py 入口 + 整个 app 包；数据文件带上
#   - alembic/  → 迁移脚本   - web_dist/ → 前端产物   - seed/ → 内置默认内容
# 并纳入 fastembed / onnxruntime / tokenizers（规则书 RAG）：模型权重不打进包，首次用到时
# 下载一次到 app-data/models（见 embedding.py）。
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = os.path.abspath(os.getcwd())
WEB_DIST = os.path.abspath(os.path.join(SPEC_DIR, "..", "apps", "web", "dist"))

datas = [
    (os.path.join(SPEC_DIR, "alembic"), "alembic"),
]
if os.path.isdir(WEB_DIST):
    datas.append((WEB_DIST, "web_dist"))
SEED = os.path.join(SPEC_DIR, "seed")
if os.path.isdir(SEED):
    datas.append((SEED, "seed"))

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

binaries = []
# RAG 依赖：collect_all 抓它们的数据文件与动态库（onnxruntime 的 .dylib、tokenizers 的
# native、fastembed 的模型清单 json 等），否则 frozen 运行时会缺文件。
for _pkg in ("fastembed", "onnxruntime", "tokenizers"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ["run_desktop.py"],
    pathex=[SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onedir：EXE 只含引导+脚本，依赖由 COLLECT 收进目录，启动时不解压 → 快。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="trpg-server",
)
