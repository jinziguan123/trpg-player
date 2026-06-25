export type SessionStatus = 'setup' | 'active' | 'paused' | 'ended'

export interface GameSession {
  id: string
  moduleId: string
  status: SessionStatus
  playerCharacterId: string
  currentSceneId: string
  worldState: Record<string, unknown>
  turnState: Record<string, unknown> | null
  createdAt: string
  updatedAt: string
}
