#!/usr/bin/env bash
# 一键构建桌面应用（macOS / Linux）：前端 → 后端 sidecar → 复制到 Tauri → 出包。
# Windows 请见 docs/packaging.md（在 Windows 机器上按步骤构建）。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
echo "==> target triple: $TRIPLE"

echo "==> [1/4] 构建前端 (vite build)"
# 直接用 vite（esbuild）出包，跳过 tsc 类型检查门禁——打包只需要产物；类型检查仍可在
# 开发/CI 用 `pnpm --filter web build`（含 tsc -b）单独跑。
pnpm --filter web exec vite build

echo "==> [2/4] 生成内置种子 + 打包后端 sidecar (PyInstaller)"
cd "$ROOT/server"
.venv/bin/python scripts/make_seed.py    # 从当前开发库导出规则书/素材/模组/角色（剔存档）
.venv/bin/pyinstaller desktop.spec --noconfirm

echo "==> [3/4] 复制 sidecar 到 Tauri（按 target triple 命名）"
mkdir -p "$ROOT/src-tauri/binaries"
BIN="trpg-server"
[ -f "dist/$BIN" ] || BIN="trpg-server.exe"   # 兼容 windows 命名
cp "dist/$(basename "$BIN")" "$ROOT/src-tauri/binaries/trpg-server-$TRIPLE${BIN##trpg-server}"
chmod +x "$ROOT/src-tauri/binaries/"trpg-server-* 2>/dev/null || true

echo "==> [4/4] Tauri 出包"
cd "$ROOT"
pnpm tauri build

echo "==> 完成，产物见 src-tauri/target/release/bundle/"
