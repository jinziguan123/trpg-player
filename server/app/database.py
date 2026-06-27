from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=settings.debug,
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
