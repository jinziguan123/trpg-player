export type EventType = 'dialogue' | 'action' | 'dice' | 'narration' | 'system'

export interface GameEvent {
  id: string
  sessionId: string
  sequenceNum: number
  eventType: EventType
  actorId: string | null
  actorName: string
  content: string
  visibility: string[]
  metadata: Record<string, unknown>
  createdAt: string
}

export interface DiceResult {
  notation: string
  rolls: number[]
  total: number
  target?: number
  outcome?: 'critical_success' | 'hard_success' | 'success' | 'failure' | 'fumble'
}

export type StreamChunkType = 'narration' | 'dialogue' | 'action' | 'dice' | 'system' | 'thinking' | 'done'

export interface StreamChunk {
  type: StreamChunkType
  actorName?: string
  content: string
  metadata: Record<string, unknown>
}
