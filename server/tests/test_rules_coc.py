import app.rules  # noqa: F401 — 触发注册

from app.rules.coc.character import (
    build_default_skills,
    compute_derived,
    roll_attributes,
)
from app.rules.coc.checks import resolve_skill_check, san_check
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


class TestCoCDamage:
    def test_apply_damage(self):
        engine = get_engine("coc")
        target = {
            "id": "test",
            "system_data": {"hitPoints": {"current": 12, "max": 12}},
        }
        result = engine.apply_damage(target, 5)
        assert result.remaining_hp == 7
        assert result.status_change is None

    def test_lethal_damage(self):
        engine = get_engine("coc")
        target = {
            "id": "test",
            "system_data": {"hitPoints": {"current": 5, "max": 12}},
        }
        result = engine.apply_damage(target, 10)
        assert result.remaining_hp == 0
        assert result.status_change == "dead"

    def test_major_wound(self):
        engine = get_engine("coc")
        target = {
            "id": "test",
            "system_data": {"hitPoints": {"current": 12, "max": 12}},
        }
        result = engine.apply_damage(target, 7)
        assert result.status_change == "major_wound"
