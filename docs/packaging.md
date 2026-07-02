# 桌面应用打包（Tauri + PyInstaller sidecar）

把本项目打成本地桌面应用。架构:

```
Tauri 外壳(Rust) ──启动──▶ 后端 sidecar(PyInstaller 打的单二进制)
   │                          └─ FastAPI(uvicorn) 同源托管前端(web_dist)+ /api + SSE
   └─ 窗口先显示 loader/ 加载页 → 轮询 /api/health 就绪 → 跳到 http://127.0.0.1:<port>
```

- 前端与后端**同源**(后端直接 serve `apps/web/dist`),免跨域/SSE 各种坑。
- 打包后数据写用户可写目录(不写只读的 .app / 安装目录):
  - macOS `~/Library/Application Support/TRPGPlayer/`
  - Windows `%APPDATA%\TRPGPlayer\`
- 规则书 RAG 的嵌入模型(fastembed bge-small-zh)**不打进包**,首次用到时联网下载到本地缓存。
- sidecar 是 onefile,**首次启动约 20s**(解压 + 重依赖导入),之后更快;loader 页会一直等到就绪。

关键文件:
- `server/run_desktop.py` — sidecar 入口(选端口起 uvicorn,打印 `TRPG_BACKEND_PORT <port>`)
- `server/desktop.spec` — PyInstaller 配置(带上 `alembic/` 迁移与 `web_dist`)
- `src-tauri/` — Tauri 外壳(`tauri.conf.json` 配 `externalBin`;`src/lib.rs` 拉起 sidecar、读端口、退出时杀进程)
- `loader/index.html` — 启动加载页

## 前置

- Rust(`cargo` / `rustc`)、Node + pnpm
- 后端 venv,并装打包依赖:`cd server && .venv/bin/pip install -e ".[packaging]"`(即 PyInstaller)
- Tauri CLI:已在根 `devDependencies`(`pnpm install` 即有);用 `pnpm tauri ...` 调用
- macOS 出 `.dmg`;Windows 出 `.msi`/NSIS `.exe`

## macOS 构建

一键:

```bash
pnpm desktop:build      # 见 scripts/build-desktop.sh
```

等价手动步骤:

```bash
pnpm --filter web build                                   # 1) 前端
cd server && .venv/bin/pyinstaller desktop.spec --noconfirm   # 2) 后端 sidecar → server/dist/trpg-server
TRIPLE=$(rustc -vV | sed -n 's/^host: //p')              # 如 aarch64-apple-darwin
mkdir -p ../src-tauri/binaries
cp dist/trpg-server ../src-tauri/binaries/trpg-server-$TRIPLE   # 3) 按 triple 命名
chmod +x ../src-tauri/binaries/trpg-server-$TRIPLE
cd .. && pnpm tauri build                                 # 4) 出包
# 产物:src-tauri/target/release/bundle/{macos/TRPG Player.app, dmg/*.dmg}
```

> 未做代码签名/公证:别人首次打开 `.app` 需右键→打开(绕过 Gatekeeper)。要分发再做 codesign + notarize。

## Windows 构建（在 Windows 机器上执行）

sidecar 二进制是平台相关的,**Windows 包必须在 Windows 上构建**:

```powershell
# 前置:Rust(MSVC toolchain)、Node+pnpm、python venv(装 .[packaging])、pnpm install
pnpm --filter web build
cd server
.venv\Scripts\pyinstaller desktop.spec --noconfirm      # → server\dist\trpg-server.exe
$triple = (rustc -vV | Select-String '^host: ').ToString().Split(' ')[1]  # x86_64-pc-windows-msvc
New-Item -ItemType Directory -Force ..\src-tauri\binaries | Out-Null
Copy-Item dist\trpg-server.exe "..\src-tauri\binaries\trpg-server-$triple.exe"
cd ..
pnpm tauri build
# 产物:src-tauri\target\release\bundle\{msi\*.msi, nsis\*.exe}
```

## 注意 / 排查

- `src-tauri/binaries/` 与 `src-tauri/gen/`、`src-tauri/target/` 均不入库,按上面步骤重建。
- 端口默认优先 8756,被占用则自动换随机端口(Rust 侧读 sidecar stdout 拿到实际端口)。
- 若窗口一直停在"正在启动本地服务":多为 sidecar 崩溃。可单独运行
  `"…app/Contents/MacOS/trpg-server"`(mac)或 `trpg-server.exe`(win)看它的报错。
- RAG:若要完全离线,需把 fastembed + onnxruntime 及模型一起打包(`desktop.spec` 里去掉
  对应 excludes 并 `--collect-all fastembed onnxruntime`),体积会明显变大——当前默认不带。
