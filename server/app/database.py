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
