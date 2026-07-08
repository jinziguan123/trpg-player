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
        logger.info("已从内置种子初始化数据到 app-data：%s", settings.db_path)
    except Exception:
        logger.exception("内置种子初始化失败（将以空库启动）")


def _alembic_config():
    """程序化 ``Config()``（不读 alembic.ini）以免 fileConfig 改写应用日志，并把
    script_location 与 db url 都钉成**绝对路径**——sidecar 可能从任意 CWD 启动，
    而 alembic.ini 里写的是相对的 ``sqlite:///trpg.db``，不能依赖它。"""
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent  # .../server
    cfg = Config()
    cfg.set_main_option("script_location", str(server_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")
    return cfg


def _db_engine():
    """按当前 ``settings.db_path`` 建一次性 engine——模块级 ``engine`` 在 import 时就绑死了
    路径，而迁移/备份要认运行时的 db_path（测试会 monkeypatch 它）。"""
    return create_engine(
        f"sqlite:///{settings.db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )


def _current_db_revision() -> str:
    """当前库的 alembic 版本号（空库/无版本表返回哨兵值），用于备份命名与差异判断。"""
    from alembic.runtime.migration import MigrationContext

    if not Path(settings.db_path).exists():
        return "none"
    eng = _db_engine()
    try:
        with eng.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
        return heads[0] if heads else "base"
    except Exception:
        return "unknown"
    finally:
        eng.dispose()


def migration_status(cfg=None) -> tuple[str, str]:
    """返回 (当前库版本, 代码 head 版本)。两者不同即代表有待应用的迁移。"""
    from alembic.script import ScriptDirectory

    cfg = cfg or _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head() or "base"
    return _current_db_revision(), head


def _backup_db_before_migration(keep: int = 2) -> Path | None:
    """迁移前把整库快照到 ``<db>.bak-<当前版本>``，只保留最近 keep 份。

    升级路径此前无任何备份：一旦迁移脚本出错，用户几十小时的存档可能被半新半旧地污染
    且不可恢复。SQLite 单文件备份成本几乎为零。先做 WAL checkpoint 让主库文件自洽再复制。
    """
    import shutil

    src = Path(settings.db_path)
    if not src.exists():
        return None
    eng = _db_engine()
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        logger.warning("迁移前 WAL checkpoint 失败，备份可能略旧", exc_info=True)
    finally:
        eng.dispose()
    dst = src.with_name(f"{src.name}.bak-{_current_db_revision()}")
    shutil.copy(src, dst)
    logger.info("迁移前已备份数据库到 %s", dst)
    backups = sorted(
        src.parent.glob(f"{src.name}.bak-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def run_migrations() -> None:
    """启动时幂等地把数据库升到最新（alembic upgrade head），升级前先自动备份。

    本项目 schema 由 alembic 管理（无 create_all）。本地/打包 sidecar 启动时自动跑，
    避免新增迁移后忘记手动升级导致 ``no such table``。已是最新则为 no-op（不备份）。

    降级保护：库版本不在代码已知迁移链内（用户用旧 app 打开了新库）时拒绝迁移并抛错，
    而非以未定义行为带病运行。
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    cfg = _alembic_config()
    current, head = migration_status(cfg)
    if current == head:
        return  # 已是最新，no-op

    # 降级场景：当前库版本不在代码的迁移链里 → 旧 app 打开了更新的库，拒绝而非乱来
    script = ScriptDirectory.from_config(cfg)
    if current not in ("none", "base"):
        known = {rev.revision for rev in script.walk_revisions()}
        if current not in known:
            raise RuntimeError(
                f"数据库版本 {current} 高于本程序已知的迁移链（head={head}），"
                "疑似用旧版本程序打开了新版本数据库；请升级程序或从备份恢复。"
            )

    _backup_db_before_migration()
    command.upgrade(cfg, "head")
