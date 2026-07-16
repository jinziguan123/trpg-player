// —— 共享数据契约（后端产出 msg.metadata.dice）与 dice-box 预定结果 notation 映射 ——
// 独立于组件文件，便于单独复用/测试，也让 DiceRoller.tsx 保持「只导出组件」以支持 Fast Refresh。

// 技能/属性检定（CoC d100）
export interface DiceCheck {
  kind: 'check'
  result: number
  tens: number[]        // 所有掷出的十位（奖励/惩罚骰会有多个），每个 ∈ {0,10,...,90}
  tens_kept: number     // 采用的十位
  units: number         // 个位 0..9
  bonus?: number
  penalty?: number
}
// 骰池（SAN 损失 / 伤害 NdM+K）
export interface DicePool {
  kind: 'pool'
  notation?: string
  dice: { sides: number; value: number }[]
  modifier?: number
  total?: number
}
export type DiceSpec = DiceCheck | DicePool

// 奖励/惩罚骰的呈现说明：本次 d100 检定是否掷了额外的十位骰，取更有利/更不利者。
// 供 3D 投掷覆盖层与结果卡「单独标出奖励/惩罚骰、并展示采用的十位与最终结果」。
export interface CheckCaption {
  kind: 'bonus' | 'penalty'
  title: string       // 「奖励骰 ×1」「惩罚骰 ×2」
  rule: string        // 「多掷的十位取更有利者」/「…更不利者」
  breakdown: string   // 「十位 00/30 → 采用 00 · 个位 5」
  result: number      // 最终 d100 点数
}

// d100 十位面文本：值 0 即“00”面（按个位可为 100 或 0X），原样显示 "00"。
function tensFace(tens: number): string {
  return tens === 0 ? '00' : String(tens)
}

/** 若本次 check 含奖励/惩罚骰则给出呈现说明，否则 null（普通检定/骰池不标注）。纯函数，便于单测。 */
export function buildCheckCaption(spec: DiceSpec): CheckCaption | null {
  if (spec.kind !== 'check') return null
  const bonus = spec.bonus || 0
  const penalty = spec.penalty || 0
  if (bonus <= 0 && penalty <= 0) return null
  const isBonus = bonus > 0
  const count = isBonus ? bonus : penalty
  const tensList = spec.tens && spec.tens.length > 0 ? spec.tens : [spec.tens_kept]
  return {
    kind: isBonus ? 'bonus' : 'penalty',
    title: `${isBonus ? '奖励骰' : '惩罚骰'} ×${count}`,
    rule: isBonus ? '多掷的十位取更有利者' : '多掷的十位取更不利者',
    breakdown: `十位 ${tensList.map(tensFace).join('/')} → 采用 ${tensFace(spec.tens_kept)} · 个位 ${spec.units}`,
    result: spec.result,
  }
}

// CoC 十位 d100：面为 10..90,00。tens 值 0（即“00”面）对应预定结果 100。
function tensToNotationValue(tens: number): number {
  return tens === 0 ? 100 : tens
}
// CoC 个位 d10：面为 1..9,0。units 0（即“0”面）对应预定结果 10。
function unitsToNotationValue(units: number): number {
  return units === 0 ? 10 : units
}

// 把契约里的骰子规格映射为 dice-box 预定结果 notation。
// **关键**：dice-box-threejs 的 parseNotation 按**第一个 `@`** 切分——`@` 之前是全部骰子、
// 之后是所有预定值（逗号分隔，按骰子出现顺序消费）。因此复合骰必须是
// 「所有骰子段用 + 连接 + 单个 @ + 全部值」：如 `1d100+1d10@40,2`、`2d6+1d8@4,5,3`。
// 早先误写成 `1d100@40+1d10@2`（多个 @）会让解析在第一个 @ 处截断、只剩一颗骰子。
// check → 十位 d100（奖励/惩罚骰可多颗）+ 个位 d10；pool → 各面数骰子落在各自 value。
export function specToNotation(spec: DiceSpec): string {
  if (spec.kind === 'check') {
    const tens = spec.tens && spec.tens.length > 0 ? spec.tens : [spec.tens_kept]
    const dicePart = `${tens.length}d100+1d10`
    const values = [...tens.map(tensToNotationValue), unitsToNotationValue(spec.units)]
    return `${dicePart}@${values.join(',')}`
  }
  // pool：按面数分组成骰子段（用 + 连接），所有值汇成末尾单个 @ 列表（顺序与骰子段一致）。
  const bySides = new Map<number, number[]>()
  for (const d of spec.dice) {
    const arr = bySides.get(d.sides) || []
    arr.push(d.value)
    bySides.set(d.sides, arr)
  }
  const diceParts: string[] = []
  const values: number[] = []
  for (const [sides, vals] of bySides) {
    diceParts.push(`${vals.length}d${sides}`)
    values.push(...vals)
  }
  return `${diceParts.join('+')}@${values.join(',')}`
}
