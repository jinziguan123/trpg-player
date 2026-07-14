from pydantic import BaseModel


class CombatActionRequest(BaseModel):
    """一个战斗行动。type: attack | dodge | fight_back | flee | first_aid | observe |
    maneuver | reload | aim | other。"""
    type: str = "attack"
    target_id: str | None = None   # first_aid 指己方受伤者、maneuver 指敌方
    weapon: str | None = None
    defense: str | None = None   # 攻击时可指定守方应对（dodge/fight_back），缺省由引擎定
    kind: str | None = None      # 机动子类型：grapple（擒抱）| disarm（缴械）
    shots: list[str] | None = None   # 连发：每发目标 id 序列（长度≥2 触发连射，截到武器射速上限）
    dest: dict | None = None     # 方格移动落点 {"x":int,"y":int}（type="move" 时用）


class ReactionRequest(BaseModel):
    """被攻击时的反应。choice: fight_back | dodge | cover。"""
    choice: str


class ChaseActionRequest(BaseModel):
    """一轮追逐行动。type: run（奔逃）| hazard（闯障，附 hazard 明细）。"""
    type: str = "run"
    hazard: dict | None = None   # {who, skill, difficulty}
