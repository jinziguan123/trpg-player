import { describe, expect, it } from 'vitest'
import { FIELD_CAP, terrainField } from './terrain'

describe('terrainField', () => {
  it('同一输入始终生成相同地形', () => {
    const input = [
      { q: 0, r: 0, biome: 'forest' },
      { q: 6, r: -2, biome: 'water', known: false },
    ]

    expect(terrainField(input)).toEqual(terrainField(input))
  })

  it('不会把场景占用格加入空域', () => {
    const field = terrainField([
      { q: 0, r: 0, biome: 'plain' },
      { q: 2, r: -1, biome: 'urban' },
    ])

    expect(field).not.toContainEqual(expect.objectContaining({ q: 0, r: 0 }))
    expect(field).not.toContainEqual(expect.objectContaining({ q: 2, r: -1 }))
  })

  it('相距很远的场景各自影响邻近空域', () => {
    const field = terrainField([
      { q: 0, r: 0, biome: 'forest' },
      { q: 20, r: 0, biome: 'desert' },
    ])

    expect(field).toContainEqual(expect.objectContaining({ q: 1, r: 0, biome: 'forest' }))
    expect(field).toContainEqual(expect.objectContaining({ q: 19, r: 0, biome: 'desert' }))
  })

  it('大包围盒结果不超过渲染上限', () => {
    const field = terrainField([
      { q: 0, r: 0, biome: 'plain' },
      { q: 100, r: 100, biome: 'mountain' },
    ])

    expect(field.length).toBeLessThanOrEqual(FIELD_CAP)
  })
})
