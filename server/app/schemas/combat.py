from pydantic import BaseModel


class CombatActionRequest(BaseModel):
    """一个战斗行动。type: attack | dodge | fight_back | flee | other。"""
    type: str = "attack"
    target_id: str | None = None
    weapon: str | None = None
    defense: str | None = None   # 攻击时可指定守方应对（dodge/fight_back），缺省由引擎定
