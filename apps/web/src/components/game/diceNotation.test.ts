import { describe, expect, it } from 'vitest'
import { buildCheckCaption, specToNotation, type DiceCheck } from './diceNotation'

// 基础 check 骰规格工厂（无奖惩，单十位）。
function check(over: Partial<DiceCheck>): DiceCheck {
  return { kind: 'check', result: 35, tens: [30], tens_kept: 30, units: 5, ...over }
}

describe('buildCheckCaption', () => {
  it('普通检定（无奖惩骰）不标注', () => {
    expect(buildCheckCaption(check({}))).toBeNull()
  })

  it('骰池不标注', () => {
    expect(buildCheckCaption({ kind: 'pool', dice: [{ sides: 6, value: 4 }] })).toBeNull()
  })

  it('奖励骰：标出取更有利的十位、列出全部十位与最终结果', () => {
    const cap = buildCheckCaption(check({ bonus: 1, tens: [30, 70], tens_kept: 30, result: 35 }))
    expect(cap).not.toBeNull()
    expect(cap!.kind).toBe('bonus')
    expect(cap!.title).toBe('奖励骰 ×1')
    expect(cap!.rule).toContain('更有利')
    expect(cap!.breakdown).toBe('十位 30/70 → 采用 30 · 个位 5')
    expect(cap!.result).toBe(35)
  })

  it('惩罚骰：取更不利的十位；多颗计数正确', () => {
    const cap = buildCheckCaption(check({ penalty: 2, tens: [10, 40, 90], tens_kept: 90, units: 3, result: 93 }))
    expect(cap!.kind).toBe('penalty')
    expect(cap!.title).toBe('惩罚骰 ×2')
    expect(cap!.rule).toContain('更不利')
    expect(cap!.breakdown).toBe('十位 10/40/90 → 采用 90 · 个位 3')
    expect(cap!.result).toBe(93)
  })

  it('“00”十位面原样显示为 00', () => {
    const cap = buildCheckCaption(check({ bonus: 1, tens: [0, 30], tens_kept: 0, units: 5, result: 5 }))
    expect(cap!.breakdown).toBe('十位 00/30 → 采用 00 · 个位 5')
  })
})

describe('specToNotation（奖惩骰多十位一并投掷）', () => {
  it('奖励骰把多颗十位与个位串成单个 @ 列表', () => {
    // tens=[30,70] → 2d100+1d10@30,70,5
    expect(specToNotation(check({ bonus: 1, tens: [30, 70], units: 5 }))).toBe('2d100+1d10@30,70,5')
  })
})
