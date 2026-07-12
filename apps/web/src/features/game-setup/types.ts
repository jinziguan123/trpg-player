export interface GameModule {
  id: string
  title: string
  description?: string
  world_setting?: Record<string, unknown> | null
}

export interface SetupCharacter {
  id: string
  name: string
  module_id: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

export interface SetupSeat {
  role: 'human' | 'ai'
  charId: string
}

export interface SessionSummary {
  id: string
  status: string
  module_title?: string
  character_name?: string
  created_at?: string
}

export interface ModuleFilters {
  query: string
  playerMin: string
  playerMax: string
  era: string
  difficulty: string
  region: string
}
