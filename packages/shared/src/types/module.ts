export interface Module {
  id: string
  title: string
  ruleSystem: 'coc' | 'dnd'
  description: string
  worldSetting: Record<string, unknown>
  scenes: Scene[]
  npcs: NpcDefinition[]
  clues: Clue[]
  createdAt: string
  updatedAt: string
}

export interface Scene {
  id: string
  title: string
  description: string
  connections: string[]
}

export interface NpcDefinition {
  id: string
  name: string
  description: string
  personality: string
  secrets: string[]
  initialLocation: string
}

export interface Clue {
  id: string
  name: string
  description: string
  location: string
  triggerCondition?: string
  discovered: boolean
}
