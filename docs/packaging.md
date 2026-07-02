# 桌面应用打包（Tauri + PyInstaller）

把本项目打成本地桌面应用。架构:

```
Tauri 外壳(Rust) ──spawn──▶ 后端(PyInstaller onedir，放在 .app 的 resources 里)
   │                          └─ FastAPI(uvicorn) 同源托管前端(web_dist)+ /api + SSE
   └─ 窗口先显示 loader/ 加载页(带进度条) → 轮询 backend_port + /api/health 就绪
      → 整窗跳到 http://127.0.0.1:<port>；退出时杀后端进程
```

- 前端与后端**同源**(后端直接 serve `apps/web/dist`),免跨域/SSE 各种坑。
- 打包后数据写用户可写目录(不写只读的 .app / 安装目录):
  - macOS `~/Library/Application Support/TRPGPlayer/`
  - Windows `%APPDATA%\TRPGPlayer\`
  - 含 `trpg.db`、`data/assets/`、`models/`(RAG 嵌入模型缓存)。
- **开箱即用**:首次启动从内置种子(`server/seed/`,由 `make_seed.py` 生成)把规则书/素材/
  模组/角色(含已算好的 RAG 向量)seed 到 app-data；已有数据则跳过。
- **RAG**:fastembed/onnxruntime 已打进包;嵌入模型权重(bge-small-zh，约百 MB)**不打进包**,
  首次用到规则书检索时下载一次到 `app-data/models`,之后复用(不每次下、不在启动路径)。
- **onedir**:启动不每次解压 → 热启约 0.6s(首次因 macOS 对未签名原生库做一次性 Gatekeeper
  扫描会慢一些)。loader 页会一直等到后端就绪。

关键文件:
- `server/run_desktop.py` — 后端入口(选端口起 uvicorn,打印 `TRPG_BACKEND_PORT <port>`)
- `server/desktop.spec` — PyInstaller onedir 配置(带 `alembic/`、`web_dist`、`seed/`;
  collect_all fastembed/onnxruntime/tokenizers)
- `server/scripts/make_seed.py` — 从当前开发库生成内置种子
- `src-tauri/` — Tauri 外壳(`tauri.conf.json` 用 `bundle.resources` 打进 onedir;
  `src/lib.rs` 从 resource 目录 spawn 后端、读端口、退出时杀进程)
- `loader/index.html` — 启动加载页(进度条)

## 前置

- Rust(`cargo` / `rustc`)、Node + pnpm
- 后端 venv,并装打包依赖:`cd server && .venv/bin/pip install -e ".[packaging]"`(即 PyInstaller)
- Tauri CLI:已在根 `devDependencies`(`pnpm install` 即有);用 `pnpm tauri ...` 调用

## macOS 构建

一键(推荐):

```bash
pnpm desktop:build      # 见 scripts/build-desktop.sh：vite → make_seed → pyinstaller(onedir) → 复制到 resources → tauri build
```

等价手动步骤:

```bash
pnpm --filter web exec vite build                         # 1) 前端(跳过 tsc 门禁，只出产物)
cd server
.venv/bin/python scripts/make_seed.py                     # 2a) 生成内置种子
.venv/bin/pyinstaller desktop.spec --noconfirm            # 2b) 后端 onedir → server/dist/trpg-server/
rm -rf ../src-tauri/resources/trpg-server
mkdir -p ../src-tauri/resources
cp -R dist/trpg-server ../src-tauri/resources/trpg-server # 3) onedir 目录 → Tauri resources
cd .. && pnpm tauri build                                 # 4) 出包
# 产物:src-tauri/target/release/bundle/{macos/TRPG Player.app, dmg/*.dmg}
```

> 未做代码签名/公证:别人首次打开 `.app` 需右键→打开(绕过 Gatekeeper)。要分发再做 codesign + notarize。

## Windows 构建（在 Windows 机器上执行）

后端产物是平台相关的,**Windows 包必须在 Windows 上构建**:

```powershell
# 前置:Rust(MSVC toolchain)、Node+pnpm、python venv(装 .[packaging])、pnpm install
pnpm --filter web exec vite build
cd server
.venv\Scripts\python scripts\make_seed.py
.venv\Scripts\pyinstaller desktop.spec --noconfirm        # → server\dist\trpg-server\（onedir 目录）
Remove-Item -Recurse -Force ..\src-tauri\resources\trpg-server -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force ..\src-tauri\resources | Out-Null
Copy-Item -Recurse dist\trpg-server ..\src-tauri\resources\trpg-server
cd ..
pnpm tauri build
# 产物:src-tauri\target\release\bundle\{msi\*.msi, nsis\*.exe}
```

> onedir 的可执行在 Windows 是 `trpg-server\trpg-server.exe`；`src/lib.rs` 里 spawn 的
> 路径对两平台通用(拼 `resources/trpg-server/trpg-server`，Windows 会自动带 .exe 后缀由
> 系统解析——如遇找不到，可在 lib.rs 里按平台补 `.exe`)。

## 注意 / 排查

- `src-tauri/resources/`、`src-tauri/gen/`、`src-tauri/target/`、`server/seed/` 均不入库,按上面步骤重建。
- 端口默认优先 8756,被占用则自动换随机端口(Rust 侧读后端 stdout 的 `TRPG_BACKEND_PORT` 拿到实际端口)。
- 若窗口一直停在加载页:多为后端崩溃。可单独运行
  `"…app/Contents/Resources/resources/trpg-server/trpg-server"`(mac)看它的报错。
- 首次用规则书检索会联网下一次嵌入模型(约百 MB)到 `app-data/models`;要完全离线分发,
  可把模型文件一并放进种子/资源并预置到该目录。
