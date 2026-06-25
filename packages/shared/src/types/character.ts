export interface Character {
  id: string
  name: string
  moduleId: string
  ruleSystem: 'coc' | 'dnd'
  isPlayer: boolean
  baseAttributes: Record<string, number>
  skills: Record<string, number>
  systemData: CoCSystemData | Record<string, unknown>
  backstory: string
  status: 'active' | 'dead' | 'incapacitated'
  createdAt: string
  updatedAt: string
}

export interface CoCSystemData {
  sanity: { current: number; max: number }
  hitPoints: { current: number; max: number }
  magicPoints: { current: number; max: number }
  luck: number
  age: number
  occupation: string
}
