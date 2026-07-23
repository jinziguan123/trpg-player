export const FIELD_MARGIN = 3
export const FIELD_CAP = 2000
/** 场景只占用视觉网格中的偶数格，给相邻场景之间留出至少一个普通地形格。 */
export const SCENE_GRID_SPACING = 2

export interface TerrainSource {
  q: number
  r: number
  biome: string
  known?: boolean
}

export interface TerrainCell {
  q: number
  r: number
  biome: string
  opacity: number
}

export interface TerrainGeometryCell {
  id: string
  q: number
  r: number
}

interface RankedTerrainCell extends TerrainCell {
  distance: number
}

/** (q,r) 对应的确定性伪随机序列；同一格每次渲染保持一致。 */
export function hexRng(q: number, r: number) {
  let seed = (q * 374761393 + r * 668265263) | 0
  return () => {
    seed = (seed ^ (seed << 13)) | 0
    seed = (seed ^ (seed >>> 17)) | 0
    seed = (seed ^ (seed << 5)) | 0
    return ((seed >>> 0) % 1000) / 1000
  }
}

export function axialDistance(a: Pick<TerrainSource, 'q' | 'r'>, b: Pick<TerrainSource, 'q' | 'r'>) {
  const dq = a.q - b.q
  const dr = a.r - b.r
  return Math.max(Math.abs(dq), Math.abs(dr), Math.abs(dq + dr))
}

/** 只追踪影响初始适配的几何信息；地貌或选中状态变化不应重置用户的缩放和平移。 */
export function terrainGeometryKey(cells: readonly TerrainGeometryCell[], width: number, height: number) {
  const points = cells
    .map((cell) => `${cell.id}:${cell.q}:${cell.r}`)
    .sort()
    .join('|')
  return `${width}x${height}|${points}`
}

/** 持久化坐标不变，仅将场景映射到更疏的前端视觉网格。 */
export function sceneDisplayCoordinate(q: number, r: number) {
  return { q: q * SCENE_GRID_SPACING, r: r * SCENE_GRID_SPACING }
}

function boundsFor(located: TerrainSource[], margin: number) {
  const qs = located.map((cell) => cell.q)
  const rs = located.map((cell) => cell.r)
  return {
    minQ: Math.min(...qs) - margin,
    maxQ: Math.max(...qs) + margin,
    minR: Math.min(...rs) - margin,
    maxR: Math.max(...rs) + margin,
  }
}

function boundsSize(bounds: ReturnType<typeof boundsFor>) {
  return (bounds.maxQ - bounds.minQ + 1) * (bounds.maxR - bounds.minR + 1)
}

/**
 * 只根据调用方传入的已落位场景生成视觉地形，不读取也不产生任何游戏语义。
 * 超过渲染上限时优先保留离场景较近的格，保证各场景周围仍有连续地貌。
 */
export function terrainField(located: TerrainSource[]): TerrainCell[] {
  const sources = located.filter((cell) => Number.isFinite(cell.q) && Number.isFinite(cell.r))
  if (sources.length === 0) return []

  let margin = FIELD_MARGIN
  while (margin > 1 && boundsSize(boundsFor(sources, margin)) > FIELD_CAP) margin -= 1
  const bounds = boundsFor(sources, margin)
  const occupied = new Set(sources.map((cell) => `${cell.q},${cell.r}`))
  const cells: RankedTerrainCell[] = []

  for (let q = bounds.minQ; q <= bounds.maxQ; q += 1) {
    for (let r = bounds.minR; r <= bounds.maxR; r += 1) {
      if (occupied.has(`${q},${r}`)) continue

      const point = { q, r }
      const random = hexRng(q, r)
      let nearest = sources[0]
      let nearestDistance = axialDistance(point, nearest)
      let nearestScore = nearestDistance + (random() - 0.5) * 0.9

      for (let i = 1; i < sources.length; i += 1) {
        const distance = axialDistance(point, sources[i])
        const score = distance + (random() - 0.5) * 0.9
        if (score < nearestScore) {
          nearest = sources[i]
          nearestDistance = distance
          nearestScore = score
        }
      }

      const fogFactor = nearest.known === false ? 0.6 : 1
      cells.push({
        q,
        r,
        biome: nearest.biome || 'plain',
        opacity: Math.max(0.06, 0.22 - 0.045 * nearestDistance) * fogFactor,
        distance: nearestDistance,
      })
    }
  }

  if (cells.length > FIELD_CAP) {
    cells.sort((a, b) => a.distance - b.distance || a.q - b.q || a.r - b.r)
    cells.length = FIELD_CAP
  }
  return cells.map(({ distance: _distance, ...cell }) => cell)
}
