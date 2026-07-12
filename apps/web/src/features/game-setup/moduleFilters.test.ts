import { describe, expect, it } from 'vitest'
import {
  createEmptyModuleFilters,
  filterModules,
  hasModuleFilters,
  parsePlayerRange,
} from './moduleFilters'
import type { GameModule } from './types'

const modules: GameModule[] = [
  {
    id: 'harbor',
    title: '雾港失灯事件',
    description: '调查港口失踪案',
    world_setting: {
      player_count: '1-3人',
      era: '现代',
      region: '中国',
      difficulty: '入门',
      tags: ['调查', '海港'],
    },
  },
  {
    id: 'manor',
    title: '旧日庄园',
    description: '一场古典恐怖冒险',
    world_setting: {
      player_count: '4-6人',
      era: '1920年代',
      region: '英国',
      difficulty: '困难',
      location: '乡间',
    },
  },
]

describe('parsePlayerRange', () => {
  it('保留单个人数值作为上限的既有行为', () => {
    expect(parsePlayerRange({ player_count: '4人' })).toEqual({ min: 1, max: 4 })
    expect(parsePlayerRange()).toEqual({ min: 1, max: 6 })
  })
})

describe('filterModules', () => {
  it('按标题、简介、标签和地点做不区分大小写的关键词匹配', () => {
    expect(filterModules(modules, { ...createEmptyModuleFilters(), query: '海港' }))
      .toEqual([modules[0]])
    expect(filterModules(modules, { ...createEmptyModuleFilters(), query: '古典恐怖' }))
      .toEqual([modules[1]])
  })

  it('组合年代、地区和难度筛选', () => {
    expect(filterModules(modules, {
      ...createEmptyModuleFilters(),
      era: '现代',
      region: '中国',
      difficulty: '入门',
    })).toEqual([modules[0]])
  })

  it('保留推荐人数与用户区间有交集的模组', () => {
    expect(filterModules(modules, {
      ...createEmptyModuleFilters(),
      playerMin: '3',
      playerMax: '4',
    })).toEqual(modules)

    expect(filterModules(modules, {
      ...createEmptyModuleFilters(),
      playerMin: '5',
      playerMax: '6',
    })).toEqual([modules[1]])
  })

  it('重置后恢复全部模组', () => {
    const reset = createEmptyModuleFilters()
    expect(hasModuleFilters(reset)).toBe(false)
    expect(filterModules(modules, reset)).toEqual(modules)
  })
})
