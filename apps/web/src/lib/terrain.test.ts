import { describe, expect, it } from 'vitest'
import {
  axialDistance,
  FIELD_CAP,
  sceneDisplayCoordinate,
  terrainGeometryKey,
  terrainField,
} from './terrain'

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

  it('相邻逻辑场景之间保留一个普通地形格', () => {
    const first = sceneDisplayCoordinate(0, 0)
    const second = sceneDisplayCoordinate(1, 0)
    const field = terrainField([
      { ...first, biome: 'urban' },
      { ...second, biome: 'forest' },
    ])

    expect(axialDistance(first, second)).toBe(2)
    expect(field).toContainEqual(expect.objectContaining({ q: 1, r: 0 }))
  })

  it('地图边缘场景的六个相邻格全部由普通地形补齐', () => {
    const source = sceneDisplayCoordinate(4, -3)
    const fieldKeys = new Set(terrainField([{ ...source, biome: 'plain' }])
      .map((cell) => `${cell.q},${cell.r}`))
    const neighbors = [
      [1, 0], [-1, 0], [0, 1], [0, -1], [1, -1], [-1, 1],
    ]

    for (const [dq, dr] of neighbors) {
      expect(fieldKeys.has(`${source.q + dq},${source.r + dr}`)).toBe(true)
    }
  })

  it('大包围盒结果不超过渲染上限', () => {
    const field = terrainField([
      { q: 0, r: 0, biome: 'plain' },
      { q: 100, r: 100, biome: 'mountain' },
    ])

    expect(field.length).toBeLessThanOrEqual(FIELD_CAP)
  })
})

describe('terrainGeometryKey', () => {
  it('忽略地貌和数组顺序，只在坐标或画布尺寸变化时改变', () => {
    const first = [
      { id: 'a', q: 0, r: 0, biome: 'plain' },
      { id: 'b', q: 2, r: -1, biome: 'forest' },
    ]
    const biomeChanged = [
      { id: 'b', q: 2, r: -1, biome: 'road' },
      { id: 'a', q: 0, r: 0, biome: 'water' },
    ]

    expect(terrainGeometryKey(first, 800, 600)).toBe(terrainGeometryKey(biomeChanged, 800, 600))
    expect(terrainGeometryKey(first, 800, 600)).not.toBe(terrainGeometryKey([
      { ...first[0], q: 1 },
      first[1],
    ], 800, 600))
    expect(terrainGeometryKey(first, 800, 600)).not.toBe(terrainGeometryKey(first, 801, 600))
  })
})
