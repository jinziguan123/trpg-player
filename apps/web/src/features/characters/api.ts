import { api } from '@/api/client'

export interface Character {
  id: string
  name: string
  module_id: string
  rule_system: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

export interface GenerateCharacterRequest {
  module_id: string
  hint: string
  is_player?: boolean
}

export function listCharacters() {
  return api.get<Character[]>('/characters')
}

export function listAvailableCharacters(isPlayer: boolean) {
  return api.get<Character[]>(
    `/characters?available=true&is_player=${isPlayer ? 'true' : 'false'}`,
  )
}

export function generateCharacter<T = Record<string, unknown>>(
  request: GenerateCharacterRequest,
) {
  return api.post<T>('/characters/ai-generate', request)
}

export function createCharacter<T = Character>(payload: unknown) {
  return api.post<T>('/characters', payload)
}

export function removeCharacter(characterId: string) {
  return api.delete(`/characters/${characterId}`)
}
