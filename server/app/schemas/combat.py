from pydantic import BaseModel


class CombatActionRequest(BaseModel):
    """一个战斗行动。type: attack | dodge | fight_back | flee | other。"""
    type: str = "attack"
    target_id: str | None = None
    weapon: str | None = None
    defense: str | None = None   # 攻击时可指定守方应对（dodge/fight_back），缺省由引擎定


class ReactionRequest(BaseModel):
    """被攻击时的反应。choice: fight_back | dodge | cover。"""
    choice: str


class ChaseActionRequest(BaseModel):
    """一轮追逐行动。type: run（奔逃）| hazard（闯障，附 hazard 明细）。"""
    type: str = "run"
    hazard: dict | None = None   # {who, skill, difficulty}
