from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.character import (
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    RollAttributesResponse,
)
from app.rules.coc.occupations import (
    COC_OCCUPATIONS,
    calc_interest_points,
    calc_occupation_points,
)
from app.services import character_service

router = APIRouter(prefix="/api", tags=["characters"])


@router.get("/rules/{rule_system}/character-schema")
def get_character_schema(rule_system: str):
    try:
        return character_service.get_character_schema(rule_system)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/rules/{rule_system}/occupations")
def get_occupations(rule_system: str):
    if rule_system != "coc":
        raise HTTPException(400, f"暂不支持 {rule_system} 的职业列表")
    return [
        {
            "name": o.name,
            "credit_min": o.credit_min,
            "credit_max": o.credit_max,
            "skill_formula": o.skill_formula,
            "skills": o.skills,
            "choices": o.choices,
        }
        for o in COC_OCCUPATIONS
    ]


class SkillPointsRequest(BaseModel):
    occupation: str
    base_attributes: dict[str, int]


@router.post("/rules/{rule_system}/calc-skill-points")
def calc_skill_points(rule_system: str, data: SkillPointsRequest):
    if rule_system != "coc":
        raise HTTPException(400, f"暂不支持 {rule_system}")
    return {
        "occupation_points": calc_occupation_points(data.occupation, data.base_attributes),
        "interest_points": calc_interest_points(data.base_attributes),
    }


@router.post("/characters/roll-attributes", response_model=RollAttributesResponse)
def roll_attributes(rule_system: str = "coc", count: int = 3):
    try:
        sets = character_service.roll_attribute_sets(rule_system, count)
        return RollAttributesResponse(sets=sets)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/characters", response_model=CharacterRead)
def create_character(data: CharacterCreate, db: Session = Depends(get_db)):
    try:
        char = character_service.create_character(db, data.model_dump())
        return char
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/characters", response_model=list[CharacterRead])
def list_characters(module_id: str | None = None, db: Session = Depends(get_db)):
    return character_service.list_characters(db, module_id)


@router.get("/characters/{character_id}", response_model=CharacterRead)
def get_character(character_id: str, db: Session = Depends(get_db)):
    char = character_service.get_character(db, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    return char


@router.put("/characters/{character_id}", response_model=CharacterRead)
def update_character(
    character_id: str, data: CharacterUpdate, db: Session = Depends(get_db)
):
    char = character_service.update_character(
        db, character_id, data.model_dump(exclude_unset=True)
    )
    if not char:
        raise HTTPException(404, "角色不存在")
    return char
