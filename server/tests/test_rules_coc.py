import app.rules  # noqa: F401 — 触发注册

from app.rules.coc.character import (
    build_default_skills,
    compute_derived,
    roll_attributes,
)
from app.rules.coc.checks import resolve_skill_check, san_check
from app.rules.coc.combat import resolve_wound
from app.rules.dice import roll
from app.rules.registry import get_engine


class TestDice:
    def test_basic_roll(self):
        result = roll("1d100")
        assert 1 <= result.total <= 100
        assert len(result.rolls) == 1

    def test_multi_dice(self):
        result = roll("3d6")
        assert 3 <= result.total <= 18
        assert len(result.rolls) == 3

    def test_modifier(self):
        result = roll("2d6+3")
        assert 5 <= result.total <= 15
        assert result.modifier == 3

    def test_negative_modifier(self):
        result = roll("1d6-1")
        assert 0 <= result.total <= 5
        assert result.modifier == -1


class TestCoCCharacter:
    def test_roll_attributes(self):
        attrs = roll_attributes()
        assert len(attrs) == 8
        for key in ["STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU"]:
            assert key in attrs
            assert 15 <= attrs[key] <= 90, f"{key}={attrs[key]} 超出范围"

    def test_derived_stats(self):
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 55, "APP": 45,
                 "INT": 70, "POW": 50, "EDU": 65}
        derived = compute_derived(attrs, age=25)

        assert derived["hitPoints"]["max"] == (60 + 65) // 10  # 12
        assert derived["magicPoints"]["max"] == 50 // 5  # 10
        assert derived["sanity"]["current"] == 50
        assert derived["sanity"]["max"] == 99
        assert derived["move"] in range(5, 10)

    def test_derived_stats_old_age(self):
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 55, "APP": 45,
                 "INT": 70, "POW": 50, "EDU": 65}
        young = compute_derived(attrs, age=25)
        old = compute_derived(attrs, age=60)
        assert old["move"] < young["move"]

    def test_build_default_skills_dodge_from_dex(self):
        # 回归：闪避取 DEX//2，母语取 EDU（DEX/EDU 在 base_attributes 里，不在 skills 里）
        skills = build_default_skills({"DEX": 70, "EDU": 65})
        assert skills["闪避"] == 35  # 70 // 2，而非旧 bug 的固定 25
        assert skills["母语"] == 65

    def test_build_default_skills_dodge_floor(self):
        # 奇数 DEX 向下取整
        assert build_default_skills({"DEX": 55})["闪避"] == 27


class TestCoCEngine:
    def test_registry(self):
        engine = get_engine("coc")
        assert engine.get_rule_system_id() == "coc"

    def test_character_schema(self):
        engine = get_engine("coc")
        schema = engine.get_character_schema()
        assert schema["rule_system"] == "coc"
        assert len(schema["attributes"]) == 8

    def test_create_character(self):
        engine = get_engine("coc")
        result = engine.create_character({"age": 30})
        assert "base_attributes" in result
        assert "skills" in result
        assert "system_data" in result
        assert result["system_data"]["age"] == 30

    def test_create_character_with_attrs(self):
        engine = get_engine("coc")
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 55,
                 "APP": 45, "INT": 70, "POW": 50, "EDU": 65}
        result = engine.create_character({"base_attributes": attrs})
        assert result["base_attributes"] == attrs

    def test_create_character_dodge_from_dex(self):
        # 回归：未提供 skills 时，闪避应为 DEX//2 而非旧 bug 的固定 25
        engine = get_engine("coc")
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 70,
                 "APP": 45, "INT": 70, "POW": 50, "EDU": 65}
        result = engine.create_character({"base_attributes": attrs})
        assert result["skills"]["闪避"] == 35
        assert result["skills"]["母语"] == 65

    def test_create_character_floors_provided_skills(self):
        # 回归：前端自带 skills（闪避=0、无母语）时，后端兜底补齐属性派生值
        engine = get_engine("coc")
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 70,
                 "APP": 45, "INT": 70, "POW": 50, "EDU": 80}
        provided = {"闪避": 0, "侦查": 60}  # 模拟前端静态默认表 + 加点
        result = engine.create_character(
            {"base_attributes": attrs, "skills": provided}
        )
        assert result["skills"]["闪避"] == 35  # DEX//2 兜底
        assert result["skills"]["母语"] == 80  # EDU 兜底
        assert result["skills"]["侦查"] == 60  # 其他技能保持不变

    def test_create_character_keeps_invested_dodge(self):
        # 玩家在基线之上加点闪避时，保留较高值（不被兜底下调）
        engine = get_engine("coc")
        attrs = {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 70,
                 "APP": 45, "INT": 70, "POW": 50, "EDU": 65}
        result = engine.create_character(
            {"base_attributes": attrs, "skills": {"闪避": 50}}
        )
        assert result["skills"]["闪避"] == 50  # max(50, 35)

    def test_validate_character(self):
        engine = get_engine("coc")
        valid_data = {
            "base_attributes": {"STR": 50, "CON": 60, "SIZ": 65, "DEX": 55,
                                "APP": 45, "INT": 70, "POW": 50, "EDU": 65}
        }
        ok, errors = engine.validate_character(valid_data)
        assert ok is True
        assert errors == []

    def test_validate_missing_attr(self):
        engine = get_engine("coc")
        ok, errors = engine.validate_character({"base_attributes": {"STR": 50}})
        assert ok is False
        assert len(errors) == 7

    def test_roll_attribute_sets(self):
        engine = get_engine("coc")
        sets = engine.roll_attribute_sets(3)
        assert len(sets) == 3
        for s in sets:
            assert len(s) == 8


class TestCoCChecks:
    def test_skill_check_range(self):
        char = {"skills": {"侦查": 60}, "base_attributes": {}}
        result = resolve_skill_check(char, "侦查")
        assert result.skill_value == 60
        assert result.target == 60
        assert 1 <= result.roll <= 100
        assert result.outcome in (
            "critical_success", "hard_success", "success", "failure", "fumble"
        )

    def test_hard_difficulty(self):
        char = {"skills": {"侦查": 60}, "base_attributes": {}}
        result = resolve_skill_check(char, "侦查", "hard")
        assert result.target == 30

    def test_extreme_difficulty(self):
        char = {"skills": {"侦查": 60}, "base_attributes": {}}
        result = resolve_skill_check(char, "侦查", "extreme")
        assert result.target == 12

    def test_san_check(self):
        char = {
            "skills": {},
            "base_attributes": {},
            "system_data": {"sanity": {"current": 50, "max": 99}},
        }
        result = san_check(char, "0", "1d6")
        assert result["old_san"] == 50
        assert result["new_san"] <= 50

    def test_san_check_fixed_integer_loss(self):
        """纯数字损失（CoC 常见的「1/1d6」写法）= 固定损失不掷骰，不得当骰式解析炸掉。"""
        char = {
            "skills": {},
            "base_attributes": {},
            "system_data": {"sanity": {"current": 50, "max": 99}},
        }
        result = san_check(char, "1", "2")   # 成败两侧都是固定值 → 损失必为 1 或 2
        assert result["san_loss"] in (1, 2)
        assert result["loss_roll"] is None                  # 固定损失不产生损失骰池
        assert result["new_san"] == 50 - result["san_loss"]

    def test_san_check_dice_loss_still_rolls(self):
        """骰式损失照常掷骰（回归护栏：整数特判不得误伤 XdY 路径）。"""
        char = {
            "skills": {},
            "base_attributes": {},
            "system_data": {"sanity": {"current": 50, "max": 99}},
        }
        result = san_check(char, "1d3", "1d6")
        assert result["loss_roll"] is not None
        assert 1 <= result["san_loss"] <= 6


class TestCoCWound:
    """伤害/重伤/濒死/死亡判定（resolve_wound，CoC 7e 权威规则）。"""

    _CD = {"skills": {}, "base_attributes": {"CON": 50}, "system_data": {}}

    def test_轻伤不改状态(self):
        r = resolve_wound(12, 12, 5, self._CD)           # 5 < 半血6
        assert r["new_hp"] == 7 and r["status"] == "ok"

    def test_单击超过满血当场死亡(self):
        r = resolve_wound(12, 12, 13, self._CD)          # 13 > max12 → 必死
        assert r["status"] == "dead" and r["new_hp"] == 0

    def test_单击恰等满血是重伤致濒死非瞬死(self):
        r = resolve_wound(12, 12, 12, self._CD)          # =max → 重伤，归零 → 濒死（非瞬死）
        assert r["status"] == "dying" and r["new_hp"] == 0

    def test_只受轻伤归零是昏迷不濒死(self):
        r = resolve_wound(4, 12, 4, self._CD)            # 单击4<半血且未曾重伤 → 昏迷稳定
        assert r["new_hp"] == 0 and r["status"] == "unconscious"

    def test_重伤归零是濒死(self):
        r = resolve_wound(6, 12, 6, self._CD)            # 单击6=半血 → 重伤，归零 → 濒死
        assert r["new_hp"] == 0 and r["status"] == "dying"

    def test_已重伤者小击归零也濒死(self):
        r = resolve_wound(3, 12, 3, self._CD, already_wounded=True)
        assert r["status"] == "dying"

    def test_重伤未归零走体质检定(self, monkeypatch):
        monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 99)  # CON 必失败
        r = resolve_wound(12, 12, 6, self._CD)           # 6=半血 → 重伤未归零 → 体质失败 → 昏迷
        assert r["status"] == "unconscious"


class TestCharacteristicRolls:
    """灵感/知识等属性骰：base_attributes 用英文键，需经别名回落。"""

    def test_idea_roll_uses_int(self):
        cd = {"skills": {"侦查": 60}, "base_attributes": {"INT": 70, "EDU": 65}}
        assert resolve_skill_check(cd, "灵感").skill_value == 70   # 灵感=INT
        assert resolve_skill_check(cd, "知识").skill_value == 65   # 知识=EDU
        assert resolve_skill_check(cd, "智力").skill_value == 70   # 中文属性名亦可
        # 普通技能不受别名影响
        assert resolve_skill_check(cd, "侦查").skill_value == 60

    def test_luck_roll_uses_system_data(self):
        """幸运不在 base_attributes（存于 system_data.luck），需单独回落。"""
        cd = {"skills": {}, "base_attributes": {"INT": 70}, "system_data": {"luck": 55}}
        assert resolve_skill_check(cd, "幸运").skill_value == 55
        assert resolve_skill_check(cd, "运气").skill_value == 55
        # 没有 system_data 时不抛错、按 0 结算
        assert resolve_skill_check({"skills": {}}, "幸运").skill_value == 0


class TestOccupationWeaponData:
    """112 职业 / 106 武器 / 专精数据完整性。"""

    def test_counts(self):
        from app.rules.coc.occupations import COC_OCCUPATIONS
        from app.rules.coc.specializations import SPECIALIZATIONS
        from app.rules.coc.weapons import COC_WEAPONS
        assert len(COC_OCCUPATIONS) == 112
        assert len(COC_WEAPONS) == 106
        assert set(SPECIALIZATIONS) == {"母语", "外语", "格斗", "射击", "科学", "生存", "技艺", "驾驶"}

    def test_every_occupation_skill_is_allocatable(self):
        """每个职业技能（按基名）要么在默认技能表，要么是专精占位——否则向导里看不到。"""
        from app.rules.coc.character import COC_DEFAULT_SKILLS
        from app.rules.coc.occupations import COC_OCCUPATIONS
        from app.rules.coc.specializations import SPECIALIZATIONS

        def base(s: str) -> str:
            return s.split("(")[0]

        allocatable = {base(k) for k in COC_DEFAULT_SKILLS} | set(SPECIALIZATIONS)
        for occ in COC_OCCUPATIONS:
            for sk in occ.skills:
                assert base(sk) in allocatable, f"{occ.name} 的技能 {sk} 不可分配"

    def test_weapon_fields_present(self):
        from app.rules.coc.weapons import COC_WEAPONS
        w = COC_WEAPONS[0]
        for key in ("name", "skill", "dam", "tho", "range", "round", "num", "err"):
            assert key in w
