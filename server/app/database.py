import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=settings.sql_echo,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """WAL + busy_timeout：让读不被写阻塞（生成在写事件时，前端拉历史仍可读），
    并发锁等待而非立即报错。多人/生成并发下的稳健性关键。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_if_needed() -> None:
    """打包运行且 app-data 尚无数据库时，从内置种子（sys._MEIPASS/seed）拷入规则书 / 素材 /
    模组 / 角色 —— 开箱即用，且规则书已带 RAG 向量、无需首次下嵌入模型。

    只在「打包(frozen) + 目标库不存在」时执行；已有数据则跳过，绝不覆盖用户数据。
    源码运行(dev)直接用 server/trpg.db，不 seed。
    """
    import shutil
    import sys

    if not getattr(sys, "frozen", False):
        return
    if Path(settings.db_path).exists():
        return
    seed = Path(sys._MEIPASS) / "seed"  # type: ignore[attr-defined]
    seed_db = seed / "trpg.db"
    if not seed_db.exists():
        return
    try:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(seed_db, settings.db_path)
        seed_assets = seed / "assets"
        if seed_assets.is_dir():
            settings.assets_dir.mkdir(parents=True, exist_ok=True)
            for f in seed_assets.iterdir():
                if f.is_file():
                    shutil.copy(f, settings.assets_dir / f.name)
        logger.info("已从内置种子初始化数据到 app-data：%s", settings.db_path)
    except Exception:
        logger.exception("内置种子初始化失败（将以空库启动）")


def run_migrations() -> None:
    """启动时幂等地把数据库升到最新（alembic upgrade head）。

    本项目 schema 由 alembic 管理（无 create_all）。本地/打包 sidecar 启动时自动跑，
    避免新增迁移后忘记手动升级导致 ``no such table``。已是最新则为 no-op。

    用程序化 ``Config()``（不读 alembic.ini）以免 fileConfig 改写应用日志，并把
    script_location 与 db url 都钉成**绝对路径**——sidecar 可能从任意 CWD 启动，
    而 alembic.ini 里写的是相对的 ``sqlite:///trpg.db``，不能依赖它。
    """
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent  # .../server
    cfg = Config()
    cfg.set_main_option("script_location", str(server_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")
    command.upgrade(cfg, "head")
