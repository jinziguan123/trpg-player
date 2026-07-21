import { describe, expect, it } from 'vitest'
import { normalizeOpposedData } from './opposedDice'

const attacker = { name: '调查员', skill: '力量', roll: 24, target: 60, outcome: 'hard_success' }
const defender = { name: '守墓人', skill: '力量', roll: 72, target: 50, outcome: 'failure' }

describe('normalizeOpposedData', () => {
  it('读取战斗和新版手动对抗的嵌套结构', () => {
    expect(normalizeOpposedData({
      opposed: { attacker, defender, winner: 'attacker', result: '调查员 胜' },
    })).toEqual({ attacker, defender, winner: 'attacker', result: '调查员 胜' })
  })

  it('兼容旧版 opposed=true + a/b + 角色名 winner', () => {
    const result = normalizeOpposedData({
      opposed: true,
      a: { actor: '调查员', skill: '力量', roll: 24, target: 60, outcome: 'hard_success' },
      b: { actor: '守墓人', skill: '力量', roll: 72, target: 50, outcome: 'failure' },
      winner: '调查员',
    })
    expect(result?.winner).toBe('attacker')
    expect(result?.attacker.name).toBe('调查员')
    expect(result?.result).toBe('调查员 胜')
  })

  it('缺少攻方结果时安全降级，不渲染对抗卡', () => {
    expect(normalizeOpposedData({ opposed: true })).toBeNull()
    expect(normalizeOpposedData({ opposed: { defender } })).toBeNull()
  })
})
