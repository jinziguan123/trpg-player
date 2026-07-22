"""KP 流式叙事协议与 NPC 台词提取状态机。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from app.services import command_protocol, world_memory
from app.services.event_protocol import make_chunk

_is_cmd_tag = command_protocol.is_command_tag
_parse_tag_kv = command_protocol.parse_tag_kv
_make_chunk = make_chunk

# 书写/标识语境：其后引号是书写/标识内容（非台词），留旁白。允许标识名词与引号间夹分隔符。
_WRITTEN_TEXT_RE = re.compile(
    r"(写着|写道|写有|刻着|刻有|记着|记载|标着|印着|贴着|挂着|题写|题着|题为|落款|显示|显现|上书|"
    r"字牌|牌子|招牌|门牌|标牌|标签|标题|铭牌|告示|名为|名叫|写作|条目|卡片|抽出一张|一行字|"
    r"短讯|电讯|报道|头条|标语|新闻|登载|刊载|载有)"
    # 线索/书写内容常带 markdown 标记或换行（如「写着：> **」「记载：\n# 」），
    # 容忍这些标点/标记夹在提示词与引号之间，避免书写内容被误抽成台词。
    r"[：:，,、\s—\-*>＞#`～~。.]*$"
)
# 感知/指称语境：其后引号是被提及/被听到的词语（非台词），留旁白。
_REFERENCE_BEFORE_RE = re.compile(
    r"(听到|听见|听过|想起|想到|提到|提及|讲到|说到|读到|看到|见到|记得|念及|称为|称作|叫做|叫作|唤作|所谓|对于|关于)[：:，,、\s]*$"
)
# 显式说话前缀：行尾「X说道：」「X：」（X 为 2-6 个中文名/称呼），用于把紧邻引号判为台词。
# 不收单字「答」——它几乎只作双字词尾（回答/答话），单收会把「修女在回答」切成「修女在回」+「答」，
# 把动词短语的半截当说话人；真正的答话由「答道/回答」或冒号形式覆盖。
_SAY_PREFIX_RE = re.compile(
    r"([一-龥·]{2,6})(?:说道|说|问道|问|答道|回答|开口道|开口|低声道|低声|喊道|叫道|笑道|沉声道|轻声道|道|：|:)[：:，,]?\s*$"
)
_SPEAK_VERB_ALT = (
    r"(?:说道|说|问道|问|答道|答|开口道|开口|低声道|低声|喊道|叫道|笑道|沉声道|轻声道|道)?"
)
# 闭引号「后面」紧跟的说话动词：用于「台词在前、说话人后置」的写法（如『“……”她说』
# 『“……”她回头对你说』）——这类现有「看引号前文」的判定抽不出说话人，会把台词漏成旁白。
# 只收明确的说话动词、去掉单字「道/问/答」等歧义词，降低把「知道/街道」误判成台词的概率。
_TRAILING_SAY_VERB_RE = re.compile(
    r"(说道|说|问道|喊道|叫道|低声道|开口道|沉声道|轻声道|笑道|叹道|回答道|回道|答道|开口)"
)
# 说话人后置且用代词时的兜底署名（判不出具名 NPC 时，用代词也好过把台词混进旁白）。
_PRONOUN_SPEAKERS = ("她", "他", "它", "您", "咱")

# 暗投/暗骰的裁定结果本应「仅 KP 可见」，但模型偶尔会把它错写进方括号泄漏给玩家
# （如 `[暗投结束 - X·心理学检定 失败]`、`【暗骰·NPC·潜行 成功】`）。这类元信息括号一律
# 丢弃、绝不回吐进旁白。匹配：含「暗投/暗骰」，或「检定」紧邻成败判词（含大成功/大失败）。
_BLIND_LEAK_RE = re.compile(
    r"暗投|暗骰|检定[^\[\]【】]{0,8}(大成功|大失败|成功|失败)"
    r"|(大成功|大失败|成功|失败)[^\[\]【】]{0,4}检定"
)


def _strip_speaker_prefix(text: str, speaker: str) -> str:
    """抹掉旁白行尾的「<说话人名>[说道]：」前缀（按完整名/局部名删，长名也不残留半截）。"""
    names = [speaker] + [p for p in speaker.split("·") if len(p) >= 2]
    for nm in sorted(names, key=len, reverse=True):
        new = re.sub(re.escape(nm) + _SPEAK_VERB_ALT + r"[：:，,]?\s*$", "", text)
        if new != text:
            return new
    return text
# 句首/小句边界后充当「主语·动作」的 NPC 名（如「诺特点点头」「史蒂芬转过身」），用于
# 在说话人以代词「他/她」承接、附近又有玩家名时，仍能把台词归给真正在行动的 NPC。
# 含逗号/分号：并列小句的主语常跟在逗号后（「格雷夫斯走进书房，霍尔护士长跟在身后」），
# 漏识别会让多说话人歧义保护（≥2 主语不猜）失效。
_SUBJECT_BOUNDARY = "。！？!?\n　 ”」』）)】，,；;"
# 名字后紧跟这些助词 → 是所有格/枚举（「科比特的…」「科比特、邓宁」），是被谈论的修饰语，
# 不是「在说话的主语」——「最近 NPC 主语」判定时不计入，避免被提及者被当说话人。
_POSSESSIVE_AFTER = "的之、和与及兼或"

def _narr_quote_span(open_q: str, buf: str, close_q: str) -> str:
    """把「没抽成对话气泡、原样留旁白」的引号片段拼回旁白：剥掉紧贴开/闭引号的换行。

    否则 KP 常写的『台词……\\n”』（闭引号另起一行）会让闭引号在旁白里孤立成一行——
    即用户报的「双引号被分到旁白中」。只剥引号首尾贴着的换行，台词内部换行（多行台词）保留。
    """
    return open_q + buf.strip("\n") + close_q


def _is_party_speaker(name: str, party_names: set[str] | None) -> bool:
    """说话人是否属于玩家党（玩家 + AI 队友）——KP 绝不能用台词气泡替他们说话/行动。

    容忍全名与名字片段互为子串（「伊芙琳」↔「伊芙琳·哈特」）；宁可偶尔挡下一个名字重叠的
    NPC，也不放过「KP 替玩家发声」——后者是最伤的沉浸感杀手。
    """
    if not party_names:
        return False
    n = (name or "").strip()
    if len(n) < 2:
        return False
    for pn in party_names:
        pn = (pn or "").strip()
        if pn and (n == pn or n in pn or pn in n):
            return True
    return False

async def filter_narration_stream(
    token_stream: AsyncIterator[str], result: list,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    guess_speakers: bool = True,
    party_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """流式输出 KP 旁白，并把 NPC 台词抽成对话气泡。

    ``party_names``（玩家 + AI 队友名）给定时，任何归到玩家党名下的台词都**不生成气泡**——
    KP 绝不能替玩家/队友发声。这是显式 [SAY] 与后置说话人路径缺失的守卫（裸引号路径本就避让玩家党）。

    ``guess_speakers=False``（结构化/say 工具路径）：**关闭裸引号的启发式说话人猜测**——
    无 [SAY] 标记的引号一律留旁白，绝不猜。对话由 say() 工具承担干净的结构化出口，
    故这里不再猜，从根上消灭「归错人」。[SAY] 显式标记仍照常识别（确定性、无歧义）。

    直接消费一个 token 流（与生成来源解耦）：旧路径喂 KPAgent.narrate 的输出，
    agent loop 路径喂 stream_chat 的文本增量——两条路径共用同一套台词抽取/
    指令剔除/流式分段逻辑。

    台词识别两条路：
    1. 显式 ``[SAY: who=<名字>]台词[/SAY]``（最可靠，用于消歧/无名角色/代词承接的说话人）。
    2. 自然引号台词（“”/「」）：据上下文判定说话人——书写/标识/被提及语境一律留旁白；
       否则按「紧邻说话前缀 → 当前说话人(承接) → 最近作为主语行动的 NPC」归属，
       都判不出且附近只有玩家名时，留旁白（不瞎猜）。

    命令标签（[DICE_CHECK] 等）仍终止本次流；[MOVE]/[GROUP] 内联剔除不终止。
    *result* = [narration, full_response, extracted, dialogue_marks, group_marks]。

    ``group_label`` 给定时（分头行动后端按组生成）：本次整段产物确定性地归入该组——
    既给流式 chunk 打上 ``metadata.group``（前端实时分栏），也以 ``(0, label)`` 预置
    group_mark（落库分组），不再依赖模型自觉打 [GROUP]。
    """
    full_response = ""
    narration = ""
    pending = ""
    in_bracket = False
    bracket_buf = ""
    bracket_open = ""   # 记录本次括号是 [ 还是【，非指令时按原样回吐
    tag_found = False

    in_say = False
    say_speaker = ""
    say_buf = ""

    in_quote = False
    quote_open = ""
    quote_buf = ""
    pending_speaker: str | None = None   # 本引号判定出的说话人（None=留旁白）
    pending_weak = False                 # 该说话人是否弱信号（仅靠最近主语推断）
    pending_from_prefix = False          # 该说话人是否来自显式「X：」前缀（强信号，内容提名不压制）
    last_speaker: str | None = None
    written_run = False                  # 处于「一串书写标识引号」中（门牌列表等），后续相邻引号同样留旁白
    gap_since_quote = ""                 # 上一处闭引号至今累计的旁白文本（判断引号是否相邻成串）
    _LIST_SEPS = " 　\t\n、,，;；/和与及·"
    quote_written = False                # 本次引号是否书写/标识内容（据此决定要不要对其做「后置说话人」判定）
    # 「说话人后置」待定台词：闭引号时判不出说话人、但内容像台词，先扣住不落旁白，
    # 看紧跟其后的文字是不是说话动词（"她说"）；是→抽成气泡，否→原样归还旁白。
    deferring = False
    deferred_open = deferred_buf = deferred_close = deferred_tail = ""

    def _looks_like_speech(text: str) -> bool:
        """像台词：有句末标点 / 口语标记 / 够长——用于过滤门牌、招牌等短名词标签。"""
        if len(text) >= 10:
            return True
        return (text and text[-1] in "。！？…?!.~～") or any(
            c in text for c in "你我吗呢吧啊呀嘛！？!?，,"
        )

    npc_matchers: list[tuple[str, list[str], bool]] = []
    for _n in (npcs or []):
        _name = _n.get("name", "")
        if not _name:
            continue
        _parts = [_name]
        for _sep in ("·", "·", " ", "-"):
            if _sep in _name:
                _parts.extend(p.strip() for p in _name.split(_sep) if len(p.strip()) >= 2)
                break
        npc_matchers.append((_name, _parts, bool(_n.get("is_player"))))

    def _canon(name: str) -> str:
        name = (name or "").strip()
        for canonical, parts, _ in npc_matchers:
            if name == canonical or name in parts:
                return canonical
        return name

    def _speaker_named_in_text(speaker: str | None, text: str) -> bool:
        """说话人名字出现在台词内容里 → 多半是「被谈论」而非「在说话」。

        典型：修女谈论科比特（『科比特藏得很深…』），启发式却把台词署名成科比特——
        被谈论者≠说话者。用于压制这类张冠李戴（仅对非显式前缀的弱判定生效）。"""
        if not speaker or not text:
            return False
        for canonical, parts, _ in npc_matchers:
            if canonical == speaker:
                return any(p in text for p in parts)
        return speaker in text

    def _prefix_speaker(s: str) -> str | None:
        """行尾「X说道：」「X：」→ 说话人（命中已知 NPC 局部名则归一；玩家方角色返回 None 抑制）。"""
        m = _SAY_PREFIX_RE.search(s)
        if not m:
            return None
        name = m.group(1)
        for canonical, parts, is_player in npc_matchers:
            if name == canonical or name in parts or name in canonical:
                return None if is_player else canonical
        # 泛称（护工/老板…）：但排除代词起头与动词短语（如「他开口」「修女在回答」），它们不是名字，
        # 交由「最近 NPC 主语」判定真正的说话人（玩家方角色会被那里排除→抑制）。
        # 含「在」= 进行体动词短语（在说/在回答/在念），是动作描写而非说话前缀，一律排除。
        if name[0] in "他她它我你咱其这那您" or any(v in name for v in "说道问答开口喊叫笑声在"):
            return None
        # 仅当该泛称是「独立称呼」——紧贴小句边界（句首/标点/换行后）才认作说话人；
        # 否则像「他指了指墙上的四个门：」这种以冒号收尾的叙述会把「墙上的四个门」误当名字。
        start = m.start(1)
        if start > 0 and s[start - 1] not in _SUBJECT_BOUNDARY:
            return None
        # 合理性校验：挡掉旁白碎片/结构指称被当泛称说话人（「第七节：」「但字距稍疏：」）
        if not world_memory.is_plausible_npc_name(name):
            return None
        return name

    def _recent_npc_subject(s: str) -> str | None:
        """最近作为「小句主语」出现的非玩家 NPC（名字紧跟在句首/句末标点后）→ 其后台词的说话人。

        窗口内出现 **≥2 个不同 NPC 主语**时返回 None：此时「取最近者」≈瞎猜（约一半会归错，
        气泡挂错名字比台词留在旁白更伤沉浸感），宁可留旁白——多说话人场景由 KP 的 [SAY]
        显式指定（prompt 已强制），不靠启发式赌。"""
        recent = s[-200:]
        best_pos, best = -1, None
        subjects: set[str] = set()
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                start = 0
                while True:
                    p = recent.find(part, start)
                    if p < 0:
                        break
                    after = recent[p + len(part): p + len(part) + 1]
                    if p == 0 or recent[p - 1] in _SUBJECT_BOUNDARY:
                        # 计入 subjects（多 NPC 在场 → 触发「≥2 不猜」保护，宁可留旁白）；
                        # 但名字后紧跟所有格/枚举助词（「科比特的遗嘱执行人」「科比特、邓宁」）时是
                        # 被谈论的修饰语/列举，不是「在说话的主语」——不作为返回的说话人。
                        subjects.add(canonical)
                        if after not in _POSSESSIVE_AFTER and p > best_pos:
                            best_pos, best = p, canonical
                    start = p + 1
        if len(subjects) >= 2:
            return None
        return best

    def _resolve_speaker(pre: str) -> tuple[str | None, bool, bool, bool]:
        """返回 (说话人, 是否弱信号, 是否来自显式前缀, 是否书写内容)。弱信号（仅靠最近 NPC
        主语推断）下，仅当引号文本「像台词」才抽取，避免把门牌/招牌等短名词标签误判为台词。
        from_prefix=True 时，调用方需把「X：」前缀从旁白里抹掉，免得说话人名重复显示。
        is_written=True 表示该引号是书写/标识内容（门牌、招牌、刻字…），留旁白。"""
        s = pre.rstrip()
        if _WRITTEN_TEXT_RE.search(s) or _REFERENCE_BEFORE_RE.search(s):
            return None, False, False, True   # 书写/标识/被提及 → 留旁白
        spk = _prefix_speaker(s)
        if spk:
            return spk, False, True, False    # 强：显式说话前缀
        if last_speaker:
            return last_speaker, False, False, False  # 强：承接当前说话人（段落分隔后会被释放）
        return _recent_npc_subject(s), True, False, False  # 弱：最近行动的 NPC 主语

    def _trailing_speaker(tail: str) -> str | None:
        """从闭引号后的文字（如「，她说」「霍尔护士长低声道」）解析后置说话人：具名优先，
        其次承接 last_speaker / 最近 NPC 主语，最后兜底用代词本身。判不出返回 None。"""
        seg = tail.lstrip("，,。、：: 　\t")
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                if seg.startswith(part):
                    return canonical
        spk = last_speaker or _recent_npc_subject(narration)
        if spk:
            return spk
        return seg[0] if seg and seg[0] in _PRONOUN_SPEAKERS else None

    extracted = result[2]
    dialogue_marks: list = result[3] if len(result) > 3 else []
    group_marks: list = result[4] if len(result) > 4 else []
    # 分头行动按组生成：确定性地把整段归入该组（流式 metadata + 落库 group_mark）。
    if group_label:
        group_marks.append((0, group_label))

    def _mk(chunk_type: str, content: str = "", **kw) -> str:
        """带分组标签的 _make_chunk：group_label 时给 chunk 附 metadata.group，供前端实时分栏。"""
        if group_label:
            md = dict(kw.pop("metadata", None) or {})
            md["group"] = group_label
            kw["metadata"] = md
        return _make_chunk(chunk_type, content, **kw)

    def _flush_pending() -> str | None:
        nonlocal pending, narration
        if not pending:
            return None
        narration += pending
        result[0] = narration
        out = pending if pending.strip() else None
        pending = ""
        return out

    def _emit_say():
        nonlocal in_say, say_speaker, say_buf, last_speaker
        # 气泡本身即「引号」，KP 若在 [SAY] 内又套了引号（[SAY]“台词”[/SAY]）会让气泡显示成
        # 「“台词」——剥掉首尾包裹的引号；台词内部的引号保留。
        text = say_buf.strip().strip("“”「」『』\"")
        speaker = _canon(say_speaker)
        in_say = False
        say_buf = ""
        say_speaker = ""
        if text and speaker:
            if _is_party_speaker(speaker, party_names):
                return None  # 绝不用气泡替玩家/队友说话：KP 误用 [SAY] 代言 → 丢弃该气泡
            last_speaker = speaker
            extracted.append((speaker, text))
            dialogue_marks.append((len(narration), speaker, text))
            return _mk("npc_dialogue", text, actor_name=speaker)
        return None

    async for token in token_stream:
        full_response += token

        for ch in token:
            if deferring:
                deferred_tail += ch
                if _TRAILING_SAY_VERB_RE.search(deferred_tail[:12]):
                    speaker = _trailing_speaker(deferred_tail)
                    text = deferred_buf.strip()
                    # 后置说话人同样压制「被台词内容点名」的张冠李戴（后置判定从不来自显式前缀）
                    if speaker and _speaker_named_in_text(speaker, text):
                        speaker = None
                    if speaker and _is_party_speaker(speaker, party_names):
                        speaker = None  # 不替玩家/队友发声
                    if speaker:
                        last_speaker = speaker
                        extracted.append((speaker, text))
                        dialogue_marks.append((len(narration), speaker, text))
                        yield _mk("npc_dialogue", text, actor_name=speaker)
                    else:
                        pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close)
                    pending += deferred_tail  # 「她说……」等引导语作旁白
                    deferring = False
                    deferred_tail = ""
                    continue
                if ch == "\n" or len(deferred_tail) > 12:
                    # 后面不是紧邻的说话动词 → 判定非台词，原样归还旁白
                    pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close) + deferred_tail
                    deferring = False
                    deferred_tail = ""
                    continue
                continue
            if in_say:
                say_buf += ch
                if say_buf.endswith("[/SAY]"):
                    say_buf = say_buf[:-len("[/SAY]")]
                    chunk = _emit_say()
                    if chunk:
                        yield chunk
                elif say_buf.endswith("\n\n"):
                    say_buf = say_buf[:-2]
                    chunk = _emit_say()
                    if chunk:
                        yield chunk
                continue

            if in_bracket:
                # 容忍全角括号【】：作为与 []] 等价的指令括号闭合
                if ch in "]】":
                    inner_s = bracket_buf.strip()
                    bracket_buf = ""
                    in_bracket = False
                    if inner_s.startswith("MOVE:") or inner_s.startswith("MOVE "):
                        continue
                    if inner_s.startswith("MAP_MARK:") or inner_s.startswith("MAP_MARK "):
                        continue
                    if inner_s.startswith("GROUP:") or inner_s.startswith("GROUP "):
                        kv = _parse_tag_kv(inner_s.split(":", 1)[-1] if ":" in inner_s else inner_s[len("GROUP"):])
                        label = (kv.get("scene") or kv.get("group") or kv.get("name") or "").strip()
                        group_marks.append((len(narration), label or None))
                        continue
                    if inner_s.startswith("SAY:") or inner_s.startswith("SAY "):
                        out = _flush_pending()
                        if out:
                            yield _mk("narration", out, actor_name="KP")
                        rest = inner_s[len("SAY"):].lstrip(": ").strip()
                        kv = _parse_tag_kv(rest)
                        say_speaker = (kv.get("who") or kv.get("name") or kv.get("speaker") or rest).strip()
                        say_buf = ""
                        in_say = True
                        continue
                    if _is_cmd_tag(inner_s):
                        tag_found = True
                        break
                    if _BLIND_LEAK_RE.search(inner_s):
                        # 暗投/暗骰裁定结果被 KP 误写进括号 → 丢弃，绝不回吐给玩家
                        continue
                    pending += bracket_open + inner_s + ("】" if bracket_open == "【" else "]")
                else:
                    bracket_buf += ch
            elif ch in "[【":
                in_bracket = True
                bracket_open = ch
                bracket_buf = ""
            elif (ch in "“「『") and not in_quote:
                # 开引号：先判说话人（基于引号前文），冲掉旁白，进入引号收集。
                # 用 narration+pending 作前文：台词常另起一段，此时前文主语（如「诺特」）
                # 已被 flush 进 narration，只看 pending 会漏掉说话人。
                # 「相邻成串」判断：上一处闭引号至今只有分隔符 → 与上一引号同属一串。
                adjacent = gap_since_quote.strip(_LIST_SEPS) == ""
                if not adjacent:
                    written_run = False
                if not guess_speakers:
                    # 结构化路径（say() 工具承担对话）：裸引号一律留旁白，绝不启发式猜说话人。
                    pending_speaker, pending_weak, from_prefix = None, False, False
                    quote_written = True
                    written_run = True
                elif written_run and adjacent:
                    # 续接书写标识串（如门牌列表）：整串都按书写内容留旁白，不抽台词。
                    pending_speaker, pending_weak, from_prefix = None, False, False
                    quote_written = True
                else:
                    pending_speaker, pending_weak, from_prefix, is_written = _resolve_speaker(narration + pending)
                    written_run = is_written
                    quote_written = is_written
                pending_from_prefix = from_prefix
                # 经显式前缀（「史蒂芬·诺特：」）判定说话人时，把该前缀从旁白里抹掉——
                # 否则说话人名会既作旁白文字、又作气泡署名，重复显示。按「完整说话人名」抹，
                # 避免长名（如「加布里埃尔·马卡里奥」）只被删掉后半截、残留「加布里埃」。
                if pending_speaker and from_prefix:
                    pending = _strip_speaker_prefix(pending, pending_speaker)
                out = _flush_pending()
                if out:
                    yield _mk("narration", out, actor_name="KP")
                in_quote = True
                quote_open = ch
                quote_buf = ""
            elif (ch in "”」』") and in_quote:
                in_quote = False
                gap_since_quote = ""        # 闭引号：重置「相邻成串」计数
                text = quote_buf.strip()
                # 弱信号下要求「像台词」，否则按标签/名词留旁白（门牌、招牌等）
                ok = bool(text and pending_speaker) and (not pending_weak or _looks_like_speech(text))
                # 非显式前缀判定的说话人若被台词内容点名（修女谈论科比特→署名科比特），压制归属
                if ok and not pending_from_prefix and _speaker_named_in_text(pending_speaker, text):
                    ok = False
                if ok and _is_party_speaker(pending_speaker, party_names):
                    ok = False  # 不替玩家/队友发声
                if ok:
                    last_speaker = pending_speaker
                    extracted.append((pending_speaker, text))
                    dialogue_marks.append((len(narration), pending_speaker, text))
                    yield _mk("npc_dialogue", text, actor_name=pending_speaker)
                elif not quote_written and text and _looks_like_speech(text):
                    # 判不出说话人、但内容像台词：先扣住不落旁白，看紧跟其后的是不是「她说」这类
                    # 后置说话人（说话人在台词之后），是则抽成气泡、否则原样归还旁白。
                    deferring = True
                    deferred_open, deferred_buf, deferred_close = quote_open, quote_buf, ch
                    deferred_tail = ""
                else:
                    pending += _narr_quote_span(quote_open, quote_buf, ch)  # 非台词：留旁白
                quote_buf = ""
                pending_speaker = None
                pending_weak = False
                pending_from_prefix = False
            else:
                if in_quote:
                    quote_buf += ch
                else:
                    pending += ch
                    gap_since_quote += ch
                    # 段落分隔＝说话的「话筒」交还：清掉 last_speaker，避免上一位说话人
                    # 跨段把后文（如另一场景里读到的报纸短讯）也吸成自己的台词；
                    # 书写标识串也在段落处中断。
                    if pending.endswith("\n\n"):
                        last_speaker = None
                        written_run = False

        if tag_found:
            out = _flush_pending()
            if out:
                yield _mk("narration", out, actor_name="KP")
            break

        if not in_bracket and not in_say and not in_quote and pending:
            while "\n\n" in pending:
                idx = pending.index("\n\n") + 2
                chunk = pending[:idx]
                pending = pending[idx:]
                narration += chunk
                result[0] = narration
                if chunk.strip():
                    yield _mk("narration", chunk, actor_name="KP")
            if len(pending) > 150:
                last_b = -1
                for _i, _ch in enumerate(pending):
                    if _ch in "\n。！？":
                        last_b = _i
                if last_b >= 0:
                    chunk = pending[: last_b + 1]
                    pending = pending[last_b + 1:]
                    narration += chunk
                    result[0] = narration
                    if chunk.strip():
                        yield _mk("narration", chunk, actor_name="KP")

    if not tag_found:
        if in_say:
            chunk = _emit_say()
            if chunk:
                yield chunk
        if in_quote:
            pending += quote_open + quote_buf.rstrip("\n")   # 未闭合引号：留旁白
        if in_bracket:
            pending += (bracket_open or "[") + bracket_buf
        if deferring:
            # 收尾仍在等后置说话人（后面没等到说话动词）：原样归还旁白
            pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close) + deferred_tail
        out = _flush_pending()
        if out:
            yield _mk("narration", out, actor_name="KP")

    result[0] = narration
    result[1] = full_response
    if len(result) > 2:
        result[2] = extracted
    if len(result) > 3:
        result[3] = dialogue_marks
    if len(result) > 4:
        result[4] = group_marks
