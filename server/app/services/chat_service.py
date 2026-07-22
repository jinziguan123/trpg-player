"""兼容旧导入路径；回合用例编排已迁至 ``turn_orchestrator``。"""

from __future__ import annotations

import sys

from app.services import turn_orchestrator as _turn_orchestrator

sys.modules[__name__] = _turn_orchestrator
