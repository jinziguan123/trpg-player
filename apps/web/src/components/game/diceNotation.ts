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

// CoC 十位 d100：面为 10..90,00。tens 值 0（即“00”面）对应预定结果 100。
function tensToNotationValue(tens: number): number {
  return tens === 0 ? 100 : tens
}
// CoC 个位 d10：面为 1..9,0。units 0（即“0”面）对应预定结果 10。
function unitsToNotationValue(units: number): number {
  return units === 0 ? 10 : units
}

// 把契约里的骰子规格映射为 dice-box 预定结果 notation（NdM@v1,v2,...）。
// check → 十位 d100（可多颗，奖励/惩罚骰）+ 个位 d10；pool → 各面数骰子落在各自 value。
export function specToNotation(spec: DiceSpec): string {
  if (spec.kind === 'check') {
    const tens = spec.tens && spec.tens.length > 0 ? spec.tens : [spec.tens_kept]
    const tensSeg = `${tens.length}d100@${tens.map(tensToNotationValue).join(',')}`
    const unitSeg = `1d10@${unitsToNotationValue(spec.units)}`
    return `${tensSeg}+${unitSeg}`
  }
  // pool：按面数分组，同面数的骰子合成一个 NdM@... 段。
  const bySides = new Map<number, number[]>()
  for (const d of spec.dice) {
    const arr = bySides.get(d.sides) || []
    arr.push(d.value)
    bySides.set(d.sides, arr)
  }
  const segs: string[] = []
  for (const [sides, values] of bySides) {
    segs.push(`${values.length}d${sides}@${values.join(',')}`)
  }
  return segs.join('+')
}
