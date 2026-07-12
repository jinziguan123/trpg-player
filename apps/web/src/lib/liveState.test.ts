import { describe, expect, it } from 'vitest'
import { parseChaseState, parseCombatState, parsePendingReaction } from './liveState'

describe('实时状态解析', () => {
  it('接受结构完整的战斗状态', () => {
    const value = { round: 1, turn: null, order: [] }
    expect(parseCombatState(value)).toEqual(value)
  })

  it('拒绝字段类型错误的战斗状态', () => {
    expect(parseCombatState({ round: '1', turn: null, order: [] })).toBeNull()
  })

  it('解析两段式投骰 pending_roll（有则带上、无则省略）', () => {
    const withRoll = parseCombatState({
      round: 2, turn: 'pc-1', order: [],
      pending_roll: { actor_id: 'pc-1', kind: 'damage', label: '投掷伤害（1D8+DB）', victim_id: 'npc-1' },
    })
    expect(withRoll?.pending_roll).toEqual({
      actor_id: 'pc-1', kind: 'damage', label: '投掷伤害（1D8+DB）', victim_id: 'npc-1',
    })
    // 无 pending_roll → 不出现该键（与旧态一致）
    expect('pending_roll' in (parseCombatState({ round: 1, turn: null, order: [] }) ?? {})).toBe(false)
    // 残缺（缺 actor_id）→ 视为无
    expect(parseCombatState({ round: 1, turn: null, order: [], pending_roll: { kind: 'damage' } })?.pending_roll).toBeUndefined()
  })

  it('只接受结构完整的反应提示', () => {
    const value = {
      attacker_id: 'npc-1',
      defender_id: 'pc-1',
      weapon: '匕首',
      ranged: false,
      allowed: ['dodge'],
      attacker_name: '陌生人',
      defender_name: '调查员',
    }
    expect(parsePendingReaction(value)).toEqual(value)
    expect(parsePendingReaction({ attacker_id: 'npc-1' })).toBeNull()
  })

  it('接受结构完整的追逐状态并拒绝残缺数据', () => {
    const value = {
      round: 1,
      gap: 2,
      escape_at: 6,
      caught_at: 0,
      quarry: '调查员',
      pursuer: '猎犬',
    }
    expect(parseChaseState(value)).toEqual(value)
    expect(parseChaseState({ round: 1, gap: 2 })).toBeNull()
  })
})
