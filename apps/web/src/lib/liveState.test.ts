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
