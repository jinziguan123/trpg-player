from app.rules.base import RuleEngine

_engines: dict[str, RuleEngine] = {}


def register_engine(engine: RuleEngine) -> None:
    _engines[engine.get_rule_system_id()] = engine


def get_engine(rule_system: str) -> RuleEngine:
    if rule_system not in _engines:
        raise ValueError(f"未注册的规则系统: {rule_system}")
    return _engines[rule_system]


def list_engines() -> list[str]:
    return list(_engines.keys())
