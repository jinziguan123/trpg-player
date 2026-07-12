import type { GameModule, ModuleFilters } from './types'

export function createEmptyModuleFilters(): ModuleFilters {
  return {
    query: '',
    playerMin: '',
    playerMax: '',
    era: '',
    difficulty: '',
    region: '',
  }
}

export function parsePlayerRange(
  worldSetting?: Record<string, unknown> | null,
): { min: number; max: number } {
  const raw = String(worldSetting?.player_count ?? '')
  const values = (raw.match(/\d+/g) ?? []).map(Number).filter((value) => value > 0)
  if (values.length >= 2) {
    return { min: Math.min(...values), max: Math.max(...values) }
  }
  if (values.length === 1) return { min: 1, max: values[0] }
  return { min: 1, max: 6 }
}

export function hasModuleFilters(filters: ModuleFilters): boolean {
  return Boolean(
    filters.query.trim()
      || filters.playerMin
      || filters.playerMax
      || filters.era
      || filters.difficulty
      || filters.region,
  )
}

export function filterModules(
  modules: GameModule[],
  filters: ModuleFilters,
): GameModule[] {
  return modules.filter((module) => {
    const world = module.world_setting ?? {}
    if (filters.era && String(world.era ?? '') !== filters.era) return false
    if (filters.region && String(world.region ?? '') !== filters.region) return false
    if (filters.difficulty && String(world.difficulty ?? '') !== filters.difficulty) return false

    const playerMin = Number.parseInt(filters.playerMin, 10)
    const playerMax = Number.parseInt(filters.playerMax, 10)
    if (!Number.isNaN(playerMin) || !Number.isNaN(playerMax)) {
      const range = parsePlayerRange(world)
      const requestedMin = Number.isNaN(playerMin) ? 1 : playerMin
      const requestedMax = Number.isNaN(playerMax) ? Number.POSITIVE_INFINITY : playerMax
      if (range.max < requestedMin || range.min > requestedMax) return false
    }

    const query = filters.query.trim().toLowerCase()
    if (!query) return true
    const tags = Array.isArray(world.tags) ? world.tags : []
    return [
      module.title,
      module.description,
      world.era,
      world.region,
      world.location,
      world.tone,
      ...tags,
    ]
      .map((value) => String(value ?? '').toLowerCase())
      .join(' ')
      .includes(query)
  })
}

export function moduleFilterOptions(modules: GameModule[]) {
  const unique = (key: 'era' | 'region') => [
    ...new Set(
      modules
        .map((module) => String(module.world_setting?.[key] ?? ''))
        .filter(Boolean),
    ),
  ]
  return { eras: unique('era'), regions: unique('region') }
}
