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


# 指令集以工具注册表为单一来源（方案二落地）：注册表各条目的方括号形态 + 文本标注
# （GROUP/SAY 非动作、未收编进注册表，此处补上）。
from app.ai.tools import REGISTRY as _TOOL_REGISTRY

KNOWN_COMMANDS = {spec.tag for spec in _TOOL_REGISTRY} | {"GROUP", "SAY", "/SAY"}
# 缺了这些参数指令必然执行失败。不整体照搬注册表的 required：部分指令的文本形态
# 允许裸值（如 [SET_FLAG hint_x]、[SCENE_CHANGE: 图书馆]），照搬会误报。
REQUIRED_ARGS = {
    "DICE_CHECK": ("skill",),
    "NPC_ACT": ("npc_id",),
    "RULE_LOOKUP": ("query",),
    "MODULE_LOOKUP": ("query",),
    "SAY": ("who",),
}

_CMD_TOKEN_RE = re.compile(r"\[(/?[A-Z][A-Z_]{2,})(?:[:：]([^\]]*))?\]")
# 叙事文本里的裸内部 id（指令参数里出现是合法的，检查前先剥指令）
_BARE_ID_RE = re.compile(r"\b(?:scene|npc|clue|trigger)_[a-z0-9_]+", re.IGNORECASE)
# 玩家消息格式回显：KP 把玩家自己的输入「[某某 行动] …」「[某某 发言] …」复读进旁白。
_EVENT_ECHO_RE = re.compile(r"\[[^\[\]]{1,20}[\s　](?:行动|发言|说道|台词)\]")

# 替玩家行动启发式：玩家名紧跟说话动词+引号 → KP 疑似替玩家角色开口。
# 前置 对/向/朝/跟/和/与 时是 NPC 对玩家说话，排除。
_SPEECH_VERBS = "说道|说|道|喊道|喊|问道|问|答道|回答|低声道|开口道|开口"
_QUOTE_OPEN = r'[「『“"]'

# 文风探针：否定式对比句式（"不是X，是Y" / "不是X而是Y" / "与其说…不如说…" /
# "这不是…，这是…"）。这是各家 LLM 头号「显得文学」的口头禅——一场用一两次是点睛，
# 密集复用则空洞、令人审美疲劳。故只测「过度复用」（一轮 ≥ 阈值次），单次合法不报。
# 逗号前的谓语段刻意排除逗号，避免「不是本地人，房子是租的」这类跨主语并列句误命中。
_ANTITHESIS_RE = re.compile(
    r"不是[^。！？；，\n]{1,30}?(?:而是|，(?:而是|却是|倒是|反倒是|反而是|才是|是))"
    r"|并非[^。！？；，\n]{1,30}?而是"
    r"|与其说[^。！？；\n]{1,30}?不如说"
    r"|这不是[^。！？；\n]{1,30}?，[^。！？；\n]{0,4}?这(?:是|才是)"
)
_TIC_DENSITY_THRESHOLD = 2  # 一轮叙述里同一修辞骨架出现达此次数即算过度复用


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


def check_event_echo(narration: str) -> list[Finding]:
    """旁白里回显了玩家消息格式「[某某 行动]/[某某 发言]」——KP 复读玩家输入，判错。"""
    return [
        Finding(
            check="event_echo", severity="error",
            detail=f"旁白回显了玩家消息格式「{m.group(0)}」",
        )
        for m in _EVENT_ECHO_RE.finditer(narration)
    ]


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


def check_antithesis_tic(narration: str) -> list[Finding]:
    """否定式对比句式过度复用探针（文风单一化）。

    仅在一轮叙述内命中 ≥ 阈值次时给 warn——单次是合法的点睛，只有密集复用才是审美疲劳的病灶。
    severity=warn：这是文风信号、不判失败，用于在 scorecard 里量化「同一修辞骨架的复用度」，
    据此验证 prompt 改动是否真的把 tic 密度压下来（而非凭感觉）。
    """
    text = _strip_commands(narration)
    hits = [m.group(0) for m in _ANTITHESIS_RE.finditer(text)]
    if len(hits) < _TIC_DENSITY_THRESHOLD:
        return []
    sample = "；".join(h[:20] for h in hits[:3])
    return [Finding(
        check="antithesis_tic", severity="warn",
        detail=f"否定式对比句式（不是X是Y）本轮出现 {len(hits)} 次，涉嫌修辞单一：{sample}",
    )]


def run_all_checks(narration: str, player_names: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_internal_ids(narration)
    findings += check_report_style(narration)
    findings += check_command_syntax(narration)
    findings += check_event_echo(narration)
    findings += check_player_control(narration, player_names)
    findings += check_antithesis_tic(narration)
    return findings
