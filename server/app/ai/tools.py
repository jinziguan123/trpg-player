"""KP 工具注册表：方括号指令体系的标准工具调用（function calling）形态。

这是 **agent loop 路径**（kp_tool_loop._run_kp_agent_loop）的单一事实来源：
每条注册项 = 一条现有方括号指令的 {工具名, OpenAI function schema, loop 行为}。
schema 的中文 description 从 prompts/kp_system.py 的指令说明提炼、保留其行为约束语义；
必填参数与旧正则路径解析的必填一致（如 dice_check 必填 skill、npc_act 必填 npc_id+trigger）。

设计取舍（相对设计稿方案二的「双轨渲染」）：**不重写手写 KP prompt 的指令说明段**——
那会伤筋动骨且需全量评估回归。旧正则路径与其手写 prompt 原样保留为降级开关；
注册表只服务 loop 路径。未来删除旧路径时，指令说明段一并改由本注册表渲染。

**不收编 SAY 与 GROUP**：SAY 是叙事文本内的台词标注、GROUP 是后端确定性归组标记，
均非「动作」，保留文本形式（loop 路径的文本流照旧由台词过滤器处理它们）。

loop 行为（kind 字段）：
- "check"  ：执行掷骰，结果 + 简短续写指引作为 tool result 回注，继续生成
  （天然取代 KP_DICE_CONTINUATION_PROMPT 的「续写」模式）；真人明骰挂「待玩家投骰」
  时中止本轮生成（suspend）。
- "lookup" ：RAG 检索，top-k 段落 + 续写指引回注（取代两套 CONTINUATION prompt）；
  rule_lookup 与 module_lookup 合计受每轮配额限制，超限由执行器返回拒绝文本。
- "npc"    ：触发 NPCAgent 生成台词、落库并广播，台词回注（KP 续写时不再复述）。
- "state"  ：fire-and-continue 的状态变更，执行后返回 "ok"。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolOutcome:
    """一次工具执行的产物：回注给模型的结果文本 + 需广播的 SSE chunk + 是否中止本轮。"""

    result_text: str
    chunks: list[str] = field(default_factory=list)
    suspend: bool = False  # True＝本轮生成就此收束（如已挂「待玩家投骰」）


@dataclass(frozen=True)
class ToolSpec:
    """一条指令的注册项：工具名 / 对应方括号指令名 / OpenAI schema / loop 行为类别。"""

    name: str        # 工具名（snake_case，即 OpenAI function name）
    tag: str         # 对应的方括号指令名（文本降级形态，如 DICE_CHECK）
    description: str
    parameters: dict  # JSON Schema（OpenAI function.parameters）
    kind: str        # "check" | "lookup" | "npc" | "state"（loop 行为，见模块 docstring）


def _params(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="dice_check",
        tag="DICE_CHECK",
        description=(
            "发起一次技能/属性检定。只在结果不确定、且成败都有意义时使用；必然成功或无关紧要"
            "的事直接叙述。调用前只描写角色「正在尝试」的过程动作，绝不预测、暗示或写出检定"
            "结果（不写「找到/发现/听到了什么」）。玩家表达观察/搜索/聆听/调查/辨认/回忆知识等"
            "感知或求知行动时必须先发起对应检定，结果回注后再按达成等级分层给信息。"
        ),
        parameters=_params({
            "skill": {
                "type": "string",
                "description": (
                    "技能名（如 侦查/聆听/图书馆使用/母语），也可为九维属性中文名"
                    "（力量/体质/体型/敏捷/外貌/智力/意志/教育/幸运；灵感=智力、知识=教育），"
                    "系统自动按属性值判定"
                ),
            },
            "difficulty": {
                "type": "string",
                "enum": ["normal", "hard", "extreme"],
                "description": "难度，默认 normal；条件明显不利用 hard，近乎极限用 extreme",
            },
            "char": {
                "type": "string",
                "description": (
                    "对谁投：缺省=主角；也可填队友名（队友行动需检定）或 NPC 名（用其数值卡）。"
                    "公共/被动感知（一声响、可触发灵感的线索——在场人人都可能注意到）填「在场」，"
                    "则在场每个玩家角色各自检定。"
                ),
            },
            "chars": {
                "type": "string",
                "description": "群检名单（多人用 / 分隔），或「在场」＝在场全体；用于公共/被动感知",
            },
            "visibility": {
                "type": "string",
                "enum": ["open", "blind"],
                "description": (
                    "默认 open 明骰（知识/语言/调查类都明骰）。仅两类用 blind："
                    "① 暗投＝角色无法自我判断对错的检定（心理学/潜意识/克苏鲁神话及暗中侦查聆听）；"
                    "② 暗骰＝NPC 背着玩家的检定（潜行/说谎，char=NPC 名）。"
                    "blind 的结果只回注给你，绝不可把成败直接告诉玩家。"
                ),
            },
            "source": {
                "type": "string",
                "description": "检定针对的具体对象/目标（如「书桌暗格」「管家的说辞」），便于结果归属",
            },
            "target": {
                "type": "string",
                "description": (
                    "仅治疗类检定（急救/医学）填：被治疗者（主角或队友名）。检定成功后由系统"
                    "确定性回血（急救回 1 点、濒死则稳住；医学回 1D3），你无需再发 HP_CHANGE。"
                ),
            },
            "bonus": {
                "type": "integer",
                "description": (
                    "奖励骰数量（缺省 0）。情境明显有利（充足时间/合适工具/队友协助等）时填 1，"
                    "系统多掷一个十位取更优。极有利可 2，但慎用。"
                ),
            },
            "penalty": {
                "type": "integer",
                "description": (
                    "惩罚骰数量（缺省 0）。情境明显不利（黑暗/受伤/干扰/时间紧迫等）时填 1，"
                    "系统多掷一个十位取更差。奖励骰与惩罚骰互相抵消，别与 difficulty 叠用来重复施压。"
                ),
            },
        }, ["skill"]),
        kind="check",
    ),
    ToolSpec(
        name="opposed_check",
        tag="OPPOSED_CHECK",
        description=(
            "对抗检定：双方各掷一次、比成功等级，同级比技能值高者胜（擒抱、追逐、潜行 vs 侦查、"
            "话术 vs 意志等双方比拼时用）。结果回注后你再据胜负续写，绝不提前写出结果。"
        ),
        parameters=_params({
            "a": {"type": "string", "description": "甲方角色名（主角/队友/NPC）"},
            "b": {"type": "string", "description": "乙方角色名（主角/队友/NPC）"},
            "skill": {"type": "string", "description": "双方共用的技能名（双方技能不同则改用 a_skill/b_skill）"},
            "a_skill": {"type": "string", "description": "甲方技能名（与 skill 二选一）"},
            "b_skill": {"type": "string", "description": "乙方技能名（缺省取 a_skill/skill）"},
        }, ["skill"]),
        kind="check",
    ),
    ToolSpec(
        name="san_check",
        tag="SAN_CHECK",
        description=(
            "理智检定（SAN）：目睹恐怖之物时对目睹者各自结算（无主角特权）。强度参考：尸体 0/1d3，"
            "血腥惨状 1/1d6，遇怪物 1/1d6，强大神话生物 1d6/1d20。系统自动掷骰算损失，勿预测。"
            "务必带 source（恐怖源标识）：系统据此保证同一角色对同一恐怖只检定一次。"
        ),
        parameters=_params({
            "success_loss": {"type": "string", "description": "成功时的 SAN 损失（骰式或数字，如 0、1d3）"},
            "failure_loss": {"type": "string", "description": "失败时的 SAN 损失（骰式或数字，如 1d6），缺省 1d6"},
            "chars": {"type": "string", "description": "目睹者名单（多人用 / 分隔），缺省在场全体"},
            "source": {"type": "string", "description": "恐怖源标识（如「墓室腐尸」），用于同源去重"},
        }, []),
        kind="check",
    ),
    ToolSpec(
        name="hp_change",
        tag="HP_CHANGE",
        description=(
            "结算 HP 变化（命中伤害或治疗恢复）。伤害 = 武器骰 + 伤害加值(DB)，随叙述结果一同发出即可。"
        ),
        parameters=_params({
            "target": {"type": "string", "description": "受伤/恢复的对象：player（主角）或队友名；NPC 不结算"},
            "delta": {"type": "integer", "description": "变化量：负数为受伤，正数为恢复"},
            "reason": {"type": "string", "description": "原因（可为空字符串）"},
        }, ["target", "delta", "reason"]),
        kind="state",
    ),
    ToolSpec(
        name="say",
        tag="SAY",
        description=(
            "让一个 NPC / 龙套说出一句台词（对话气泡）。**任何 NPC 开口说话都要调用本工具**，"
            "不要把台词写进叙述文本里——写进文本的引号不会被识别为气泡。who 填说话人完整姓名，"
            "text 只填这句话本身（不要带引号、不要带「谁说」等描述）。多句/多人轮流各自调用一次。"
            "台词会直接展示给玩家，续写时不要复述。旁白（环境、动作、神态）仍照常写进叙述文本。"
            " 严格限制：① **只用于当前在场的 NPC / 龙套，绝不能替玩家角色或 AI 队友说话或行动**——"
            "他们的言行只由玩家本人给出，你替他们发声会被系统拒绝；② text 必须是**真正说出口的话**，"
            "绝不要拿印章文字、文档/卷宗内容、旁白、动作描述来调用本工具（那些写进叙述文本）；"
            "③ 不在场的人不能说话。"
        ),
        parameters=_params({
            "who": {"type": "string", "description": "说话人完整姓名（NPC 或临场龙套）"},
            "text": {"type": "string", "description": "台词内容本身，不含引号与「谁说」等描述"},
        }, ["who", "text"]),
        kind="npc",
    ),
    ToolSpec(
        name="start_combat",
        tag="START_COMBAT",
        description=(
            "开打时调用：把当前场景切入**结构化战斗轮**（先攻队列、逐轮结算、引擎掷伤害）。"
            "enemies 填参战的敌方名字（模组 NPC 名或临场敌，逗号分隔）；trigger 一句话说明因何开打。"
            "调用后战斗由系统按先攻推进（真人轮到时前端提示其操作），你**本轮就此收束、不要再叙述战斗过程**——"
            "战斗结束后系统会把结果摘要回灌给你，你再续写余波。仅在真正进入你死我活的交战时用。"
        ),
        parameters=_params({
            "enemies": {"type": "string", "description": "参战敌方名字，逗号分隔（模组 NPC 名或临场敌）"},
            "trigger": {"type": "string", "description": "因何开打的一句话"},
        }, ["enemies"]),
        kind="state",
    ),
    ToolSpec(
        name="start_chase",
        tag="START_CHASE",
        description=(
            "有人开始逃跑/被追时调用：切入**抽象距离轨追逐**。pursuer 填追方名字（模组 NPC 或临场），"
            "玩家默认作逃方。调用后追逐由系统逐轮推进（真人点『奔逃』推进一轮，越阈值判脱身/被追上），"
            "你**本轮就此收束**，追逐结束后系统回灌结果摘要，你再续写。"
        ),
        parameters=_params({
            "pursuer": {"type": "string", "description": "追方名字（模组 NPC 名或临场追兵）"},
            "trigger": {"type": "string", "description": "因何开始追逐的一句话"},
        }, ["pursuer"]),
        kind="state",
    ),
    ToolSpec(
        name="npc_act",
        tag="NPC_ACT",
        description=(
            "让某个 NPC 在场景触发下自主行动/开口：由该 NPC 的人格代理生成台词、直接展示给玩家。"
            "台词会回注给你——续写时不要复述它，保持 NPC 言行符合其性格与知识范围。"
        ),
        parameters=_params({
            "npc_id": {"type": "string", "description": "NPC 的内部 id（见 NPC 列表）"},
            "trigger": {"type": "string", "description": "触发情境（该 NPC 为何此刻行动/开口）"},
        }, ["npc_id", "trigger"]),
        kind="npc",
    ),
    ToolSpec(
        name="scene_change",
        tag="SCENE_CHANGE",
        description=(
            "切换当前场景：仅当玩家明确移动、真到了别处（进屋/前往某地）时调用；"
            "别因其讨论/打算就搬人（「该先去X」只是商量）。"
        ),
        parameters=_params({
            "scene_id": {"type": "string", "description": "目标场景的 id（或场景名，系统会解析）"},
        }, ["scene_id"]),
        kind="state",
    ),
    ToolSpec(
        name="rule_lookup",
        tag="RULE_LOOKUP",
        description=(
            "查阅规则书原文：对某条具体规则的精确裁定没把握时（伤害结算、特殊检定、战斗细则、"
            "技能用法、法术/神话生物效果）调用，系统返回最相关的原文片段供你裁定。"
            "拿不准就查——要引用具体数值/流程而上文规则要点没写明的，宁可查一次也别凭印象编"
            "（与 module_lookup 合计每轮最多 2 次）；这是内部动作，"
            "不要把「我去翻书」讲给玩家听，更不要借机透露线索。"
        ),
        parameters=_params({
            "query": {"type": "string", "description": "要查的规则关键词或问题"},
        }, ["query"]),
        kind="lookup",
    ),
    ToolSpec(
        name="module_lookup",
        tag="MODULE_LOOKUP",
        description=(
            "查阅模组原文：需要模组原文的确切细节时（场景描写的原有笔力、NPC 台词或信件/铭文原文、"
            "作者的具体设定）调用，系统返回最相关的原文片段。只在确有必要时查（与 rule_lookup "
            "合计每轮最多 2 次）；查到的原文可能含玩家尚未触及的内容，泄密约束照常适用。"
        ),
        parameters=_params({
            "query": {"type": "string", "description": "要查的场景/人物/细节关键词"},
        }, ["query"]),
        kind="lookup",
    ),
    ToolSpec(
        name="set_flag",
        tag="SET_FLAG",
        description=(
            "置剧情标志：叙事中确实发生了「剧情推进指引」所述的关键转折时调用，系统据此把相关"
            "场景/NPC 切到新样貌，你后续叙述必须与之一致。这是内部控制、玩家不可见；"
            "只在剧情真的推进到该节点时发，绝不无中生有、不滥用。"
        ),
        parameters=_params({
            "flag": {"type": "string", "description": "标志名（见剧情推进指引）"},
        }, ["flag"]),
        kind="state",
    ),
    ToolSpec(
        name="clear_flag",
        tag="CLEAR_FLAG",
        description="清剧情标志：某状态/危险确实消退时调用（与 set_flag 相对，同样的克制约束）。",
        parameters=_params({
            "flag": {"type": "string", "description": "标志名"},
        }, ["flag"]),
        kind="state",
    ),
    ToolSpec(
        name="handout",
        tag="HANDOUT",
        description=(
            "发放手书（信件/报纸/日记/便条等实体文书）：剧情确实达成某份手书的发放条件（玩家搜到、"
            "被交予、读到）时调用，系统把原文以卡片发给全桌。只发可发放清单里列出的 id；"
            "每份只发一次；不要在叙述里替玩家朗读、转述或改写正文——你只描述玩家如何拿到它。"
        ),
        parameters=_params({
            "id": {"type": "string", "description": "手书 id（见可发放清单）"},
        }, ["id"]),
        kind="state",
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in REGISTRY}
TAG_TO_TOOL: dict[str, str] = {spec.tag: spec.name for spec in REGISTRY}


def openai_schema(spec: ToolSpec) -> dict:
    """单条注册项 → OpenAI function schema。"""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def openai_tool_schemas(exclude: set[str] | None = None) -> list[dict]:
    """全部（或剔除 exclude 后的）注册项渲染成 stream_chat 的 tools 列表。

    exclude 用于按运行时能力裁剪：未挂规则书时剔除 rule_lookup、模组原文索引
    未就绪时剔除 module_lookup（镜像旧路径「不广告该能力」的门槛）。
    """
    exclude = exclude or set()
    return [openai_schema(spec) for spec in REGISTRY if spec.name not in exclude]


def render_tag(spec: ToolSpec, arguments: dict | None) -> str:
    """把一次工具调用还原成方括号指令文本（评估序列化 / 日志用）。"""
    args = {k: v for k, v in (arguments or {}).items() if v is not None and str(v) != ""}
    if not args:
        return f"[{spec.tag}]"
    inner = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"[{spec.tag}: {inner}]"


def tool_mode_message() -> dict:
    """loop 路径追加的系统消息：把手写 prompt 里的方括号指令映射到工具调用。

    不重写手写指令说明段（各指令的时机与约束照旧生效），只声明表达形式的切换；
    对话改用 say() 工具（结构化出口，从根上取消台词的启发式解析）；GROUP 仍是文本标注。
    """
    return {
        "role": "system",
        "content": (
            "【工具调用模式】本会话已启用标准工具调用：上文提到的方括号指令"
            "（[DICE_CHECK]、[OPPOSED_CHECK]、[SAN_CHECK]、[HP_CHANGE]、[NPC_ACT]、"
            "[SCENE_CHANGE]、[RULE_LOOKUP]、[MODULE_LOOKUP]、[SET_FLAG]、[CLEAR_FLAG]、"
            "[HANDOUT]）一律改为调用对应的同名工具（dice_check、opposed_check、"
            "san_check、hp_change、npc_act、scene_change、rule_lookup、module_lookup、"
            "set_flag、clear_flag、handout），不要把这些指令写成文本。"
            "**NPC 台词一律改为调用 say(who, text) 工具**（每句一次，who 填说话人姓名、"
            "text 只填这句话本身）——不要再把台词或 [SAY] 写进叙述文本，写进文本的引号不会"
            "成为对话气泡。旁白（环境/动作/神态）仍照常写在叙述文本里。"
            "严禁在叙述文本里写形如 `名字：「台词」` 或 `名字（身份）：「台词」` 的署名台词——"
            "那属于漏调工具，会被当旁白原样渲染（还可能重复人名）。凡有 NPC 开口,就调 say()。"
            "反例(不要这样写)：叙述里出现『京山（乘务员）：“它是死的吗？”』；"
            "正例：叙述只写『京山张了张嘴，声音压得极低。』,那句台词改用 say(who=\"京山\", text=\"它是死的吗？\")。"
            "**绝不用 say() 替玩家角色或 AI 队友说话/行动**（那是他们本人的权利，系统会拒绝）；"
            "**绝不拿非台词内容（印章、卷宗/文档文字、旁白、动作描述）去调 say()**。"
            "各指令的使用时机与行为约束（何时检定、何时暗投、不预测结果、不泄密等）完全不变。"
            "工具执行结果会回注给你，你据此继续叙述。发起检定（dice_check/opposed_check）后"
            "请立即结束本段输出，等待结果回注。例外：[GROUP: scene=…] 仍是文本标注、照旧写在叙述里。"
        ),
    }
