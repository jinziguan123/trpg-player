from app.rules.coc.engine import CoCRuleEngine
from app.rules.registry import register_engine

register_engine(CoCRuleEngine())
