"""生成打包用的「种子数据」：从当前开发库导出规则书 / 素材 / 模组 / 角色（含已算好的 RAG
向量），剔除游戏存档（会话 / 事件 / 参与者）。打包时被 PyInstaller 带进去，打包 app **首次
启动**（app-data 尚无库）时 seed 进去 —— 开箱即用，且已带向量、无需首次下嵌入模型。

用法：cd server && .venv/bin/python scripts/make_seed.py
产物：server/seed/trpg.db + server/seed/assets/*
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"
# 剔除的「游戏存档」表：种子只保留可复用的内容（规则书/素材/模组/角色）。
STRIP_TABLES = ["event_logs", "session_participants", "game_sessions"]


def main() -> None:
    src_db = Path(settings.db_path)
    if not src_db.exists():
        print(f"[make_seed] 源库不存在：{src_db}")
        sys.exit(1)

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    seed_db = SEED_DIR / "trpg.db"
    seed_db.unlink(missing_ok=True)

    # 用 sqlite backup API 而非文件拷贝：正确处理 WAL，拿到一致快照。
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(seed_db))
    dst.execute("PRAGMA journal_mode=DELETE")  # 种子不带 -wal 附属文件
    src.backup(dst)
    src.close()

    cur = dst.cursor()
    for t in STRIP_TABLES:
        try:
            cur.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    dst.commit()
    cur.execute("VACUUM")
    counts = {}
    for t in ("rulebooks", "rule_chunks", "assets", "modules", "characters"):
        try:
            counts[t] = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[t] = "-"
    dst.close()

    # 素材文件
    seed_assets = SEED_DIR / "assets"
    if seed_assets.exists():
        shutil.rmtree(seed_assets)
    n_assets = 0
    if Path(settings.assets_dir).is_dir():
        shutil.copytree(settings.assets_dir, seed_assets)
        n_assets = sum(1 for _ in seed_assets.iterdir())

    print(f"[make_seed] 种子已生成：{seed_db}")
    print(f"[make_seed] 内容：{counts}；素材文件：{n_assets}")


if __name__ == "__main__":
    main()
