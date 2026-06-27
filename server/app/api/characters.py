import json

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.models.module import Module
from app.schemas.character import (
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    RollAttributesResponse,
)
from app.rules.coc.equipment import get_available_equipment
from app.rules.coc.occupations import (
    COC_OCCUPATIONS,
    calc_interest_points,
    calc_occupation_points,
)
from app.services import ai_character_service, character_service, session_service
from app.services.excel_import import parse_coc_character_sheet

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


@router.get("/rules/{rule_system}/equipment")
def get_equipment(rule_system: str, era: str = "1920s", credit_rating: int = 0):
    if rule_system != "coc":
        raise HTTPException(400, f"暂不支持 {rule_system}")
    return get_available_equipment(era, credit_rating)


class EvaluateRequest(BaseModel):
    module_id: str
    name: str
    occupation: str = ""
    backstory: str = ""


@router.post("/characters/evaluate")
async def evaluate_character(data: EvaluateRequest, db: Session = Depends(get_db)):
    module = db.get(Module, data.module_id)
    if not module:
        raise HTTPException(404, "模组不存在")

    era = (module.world_setting or {}).get("era", "未知")
    era_tag = (module.world_setting or {}).get("era", "")

    from app.ai.deepseek import get_llm
    llm = get_llm()
    prompt = (
        f"你是一个 TRPG 角色审核专家。请评估以下角色是否适合参与指定的模组。\n\n"
        f"模组信息：\n- 标题：{module.title}\n- 年代：{era_tag or era}\n"
        f"- 描述：{module.description}\n\n"
        f"角色信息：\n- 名字：{data.name}\n- 职业：{data.occupation or '未知'}\n"
        f"- 背景故事：{data.backstory or '无'}\n\n"
        f"请检查：\n"
        f"1. 角色的职业和背景是否符合模组的时代背景（例如1920s不应有现代科技产物）\n"
        f"2. 背景中提到的物品、装备是否在该时代合理\n"
        f"3. 角色概念是否与模组基调匹配\n\n"
        f'以 JSON 格式返回：\n{{"compatible": true或false, "warnings": ["不合理之处"], "suggestions": ["建议"]}}\n'
        f"如果没有问题，warnings 和 suggestions 为空数组。"
    )
    result = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=1024,
    )
    return json.loads(result)


@router.post("/characters/import-excel")
async def import_from_excel(
    module_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.endswith(".xlsx"):
        raise HTTPException(400, "请上传 .xlsx 格式的 Excel 文件")

    content = await file.read()
    try:
        parsed = parse_coc_character_sheet(content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Excel 解析失败，请确认是 COC 七版角色卡格式")

    return {
        **parsed,
        "module_id": module_id,
        "rule_system": "coc",
    }


class AIGenerateRequest(BaseModel):
    module_id: str
    hint: str = ""
    is_player: bool = False


@router.post("/characters/ai-generate")
async def ai_generate_character(data: AIGenerateRequest, db: Session = Depends(get_db)):
    try:
        result = await ai_character_service.generate_ai_character(
            db, data.module_id, data.hint, data.is_player,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(502, "AI 生成失败，请重试")
    return {**result, "module_id": data.module_id, "rule_system": "coc"}


@router.post("/characters/roll-attributes", response_model=RollAttributesResponse)
def roll_attributes(rule_system: str = "coc", count: int = 3):
    try:
        sets = character_service.roll_attribute_sets(rule_system, count)
        return RollAttributesResponse(sets=sets)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/characters", response_model=CharacterRead)
def create_character(
    data: CharacterCreate,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    try:
        # 真人角色绑定到创建者 token；AI 角色（is_player=false）不绑定
        payload = data.model_dump()
        if data.is_player and token:
            payload["owner_token"] = token
        char = character_service.create_character(db, payload)
        return char
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/characters", response_model=list[CharacterRead])
def list_characters(
    module_id: str | None = None,
    available: bool = False,
    is_player: bool | None = None,
    mine: bool = False,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    chars = character_service.list_characters(db, module_id)
    if is_player is not None:
        chars = [c for c in chars if c.is_player == is_player]
    if mine:
        # 仅返回当前 token 拥有的角色（认领席位时用）
        chars = [c for c in chars if c.owner_token and c.owner_token == token]
    if available:
        occupied = session_service.active_character_ids(db)
        chars = [c for c in chars if c.id not in occupied]
    return chars


@router.get("/characters/{character_id}", response_model=CharacterRead)
def get_character(character_id: str, db: Session = Depends(get_db)):
    char = character_service.get_character(db, character_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    return char


@router.delete("/characters/{character_id}")
def delete_character(character_id: str, db: Session = Depends(get_db)):
    if not character_service.delete_character(db, character_id):
        raise HTTPException(404, "角色不存在")
    return {"ok": True}


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
