"""确定性检查（免费，不调 LLM）：内部标识泄漏 / 汇报体 / 指令语法 / 替玩家行动启发式。

severity 语义：error = 计入不通过；warn = 启发式命中，仅提示、不判失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.turn_validator import _INTERNAL_ID_RE, _REPORT_STYLE_RE


@dataclass
class Finding:
    check: str
    severity: str  # "error" | "warn"
    detail: str

    def to_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity, "detail": self.detail}


# 与 chat_service 的指令集保持一致；新增指令时同步维护（方案二注册表落地后改为单一来源）。
KNOWN_COMMANDS = {
    "DICE_CHECK", "OPPOSED_CHECK", "SAN_CHECK", "HP_CHANGE", "NPC_ACT",
    "SCENE_CHANGE", "RULE_LOOKUP", "SET_FLAG", "CLEAR_FLAG", "MOVE",
    "GROUP", "SAY", "/SAY",
}
# 缺了这些参数指令必然执行失败
REQUIRED_ARGS = {
    "DICE_CHECK": ("skill",),
    "NPC_ACT": ("npc_id",),
    "RULE_LOOKUP": ("query",),
    "SAY": ("who",),
}

_CMD_TOKEN_RE = re.compile(r"\[(/?[A-Z][A-Z_]{2,})(?:[:：]([^\]]*))?\]")
# 叙事文本里的裸内部 id（指令参数里出现是合法的，检查前先剥指令）
_BARE_ID_RE = re.compile(r"\b(?:scene|npc|clue|trigger)_[a-z0-9_]+", re.IGNORECASE)

# 替玩家行动启发式：玩家名紧跟说话动词+引号 → KP 疑似替玩家角色开口。
# 前置 对/向/朝/跟/和/与 时是 NPC 对玩家说话，排除。
_SPEECH_VERBS = "说道|说|道|喊道|喊|问道|问|答道|回答|低声道|开口道|开口"
_QUOTE_OPEN = r'[「『“"]'


def _strip_commands(narration: str) -> str:
    return _CMD_TOKEN_RE.sub(" ", narration)


def check_internal_ids(narration: str) -> list[Finding]:
    text = _strip_commands(narration)
    findings = []
    for regex in (_INTERNAL_ID_RE, _BARE_ID_RE):
        for m in regex.finditer(text):
            findings.append(Finding(
                check="internal_ids", severity="error",
                detail=f"旁白出现内部标识「{m.group(0)}」",
            ))
    return findings


def check_report_style(narration: str) -> list[Finding]:
    if _REPORT_STYLE_RE.search(narration):
        return [Finding(
            check="report_style", severity="error",
            detail="出现【标题】+ 项目符号列表的汇报体段落",
        )]
    return []


def check_command_syntax(narration: str) -> list[Finding]:
    findings = []
    for m in _CMD_TOKEN_RE.finditer(narration):
        name, args = m.group(1), m.group(2) or ""
        if name not in KNOWN_COMMANDS:
            findings.append(Finding(
                check="command_syntax", severity="error",
                detail=f"未知指令 [{name}]",
            ))
            continue
        required = REQUIRED_ARGS.get(name)
        if required:
            keys = {
                kv.split("=", 1)[0].strip()
                for kv in args.split(",") if "=" in kv
            }
            missing = [k for k in required if k not in keys]
            if missing:
                findings.append(Finding(
                    check="command_syntax", severity="error",
                    detail=f"[{name}] 缺少必要参数 {missing}（原文：{m.group(0)}）",
                ))
    if narration.count("[SAY") != narration.count("[/SAY]"):
        findings.append(Finding(
            check="command_syntax", severity="warn",
            detail="[SAY] 与 [/SAY] 数量不配对",
        ))
    return findings


def check_player_control(narration: str, player_names: list[str]) -> list[Finding]:
    text = _strip_commands(narration)
    findings = []
    for name in player_names:
        if not name:
            continue
        pattern = re.compile(
            rf"(.)?{re.escape(name)}\s*(?:{_SPEECH_VERBS})\s*[:：]?\s*{_QUOTE_OPEN}"
        )
        for m in pattern.finditer(text):
            prefix = m.group(1) or ""
            if prefix and prefix in "对向朝跟和与":
                continue  # NPC 对玩家说话
            findings.append(Finding(
                check="player_control", severity="warn",
                detail=f"疑似替玩家角色「{name}」开口：…{m.group(0)[:30]}…",
            ))
    return findings


def run_all_checks(narration: str, player_names: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_internal_ids(narration)
    findings += check_report_style(narration)
    findings += check_command_syntax(narration)
    findings += check_player_control(narration, player_names)
    return findings
