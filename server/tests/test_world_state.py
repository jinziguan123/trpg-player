"""world_state 读写适配器 + schema 版本单测。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, GameSession, Module  # noqa: F401 —— 注册建表
from app.services import world_state as ws_mod


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ws.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    m = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    db.add(m); db.flush()
    s = GameSession(module_id=m.id, status="active", world_state={})
    db.add(s); db.commit()
    return s


def test_set_key_persists_and_stamps_version(db_factory):
    db = db_factory(); s = _seed(db)
    ws_mod.set_key(db, s, "combat", {"active": True})
    s2 = db_factory().get(GameSession, s.id)
    assert s2.world_state["combat"] == {"active": True}
    assert s2.world_state["schema_version"] == ws_mod.SCHEMA_VERSION


def test_set_key_none_deletes(db_factory):
    db = db_factory(); s = _seed(db)
    ws_mod.set_key(db, s, "combat", {"active": True})
    ws_mod.set_key(db, s, "combat", None)
    assert "combat" not in (db_factory().get(GameSession, s.id).world_state or {})


def test_mutate_persists_nested_change(db_factory):
    """核心：mutate 深拷贝后改**嵌套值**，重载仍在——规避「改旧值判无变化不落库」的坑。"""
    db = db_factory(); s = _seed(db)
    ws_mod.set_key(db, s, "combat", {"round": 1, "hp": {"e1": 10}})

    def _bump(w):
        w["combat"]["hp"]["e1"] = 3

    ws_mod.mutate(db, s, _bump)
    assert db_factory().get(GameSession, s.id).world_state["combat"]["hp"]["e1"] == 3


def test_read_returns_detached_deepcopy(db_factory):
    db = db_factory(); s = _seed(db)
    ws_mod.set_key(db, s, "combat", {"round": 1})
    snap = ws_mod.read(s)
    snap["combat"]["round"] = 99                     # 改快照
    assert s.world_state["combat"]["round"] == 1     # 不影响 ORM 挂着的值


def test_get_reads_key(db_factory):
    db = db_factory(); s = _seed(db)
    ws_mod.set_key(db, s, "flag", "on")
    assert ws_mod.get(s, "flag") == "on"
    assert ws_mod.get(s, "missing", "def") == "def"


def test_migrate_stamps_version():
    out = ws_mod.migrate({"foo": 1})
    assert out["schema_version"] == ws_mod.SCHEMA_VERSION and out["foo"] == 1
