import { create } from 'zustand'
import { api } from '../api/client'

export interface SessionParticipant {
  character_id: string | null
  role: string // human | ai
  is_primary: boolean
  seat_order: number
  claimed: boolean
  is_mine: boolean
  character_name?: string | null
}

export interface ParticipantInput {
  character_id: string | null
  role: string
  is_primary: boolean
}

interface GameSession {
  id: string
  module_id: string
  status: string
  player_character_id: string | null
  room_code?: string | null
  current_scene_id: string | null
  world_state: Record<string, unknown>
  participants?: SessionParticipant[]
  module_title?: string
  character_name?: string
  created_at?: string
}

interface ChatMessage {
  id: string
  type: string
  content: string
  actor_name?: string
  metadata?: Record<string, unknown>
  sequence_num?: number
}

interface EventPayload {
  id: string
  sequence_num: number
  event_type: string
  actor_id: string | null
  actor_name: string
  content: string
  metadata_: Record<string, unknown>
}

interface EventsResponse {
  events: EventPayload[]
  has_more: boolean
}

interface SessionStore {
  sessions: GameSession[]
  currentSession: GameSession | null
  messages: ChatMessage[]
  loading: boolean
  hasMoreHistory: boolean
  loadingOlder: boolean
  streamingMsgId: string | null
  fetchSessions: () => Promise<void>
  createSession: (moduleId: string, participants: ParticipantInput[]) => Promise<GameSession>
  setCurrentSession: (session: GameSession) => void
  addMessage: (msg: ChatMessage) => void
  startStreamMessage: (type: string, actorName?: string) => string
  appendToStream: (content: string) => void
  endStream: () => void
  replaceLastNarration: (content: string) => void
  clearMessages: () => void
  loadHistory: (sessionId: string) => Promise<void>
  loadOlderEvents: (sessionId: string) => Promise<void>
}

let msgCounter = 0

function eventsToMessages(events: EventPayload[], playerCharId: string | null): ChatMessage[] {
  return events.map((e) => ({
    id: e.id,
    type: e.event_type,
    content: e.content,
    actor_name: e.actor_name,
    sequence_num: e.sequence_num,
    metadata: { ...e.metadata_, is_player: !!(playerCharId && e.actor_id === playerCharId) },
  }))
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  sessions: [],
  currentSession: null,
  messages: [],
  loading: false,
  hasMoreHistory: false,
  loadingOlder: false,
  streamingMsgId: null,

  fetchSessions: async () => {
    const sessions = await api.get<GameSession[]>('/sessions')
    set({ sessions })
  },

  createSession: async (moduleId, participants) => {
    const session = await api.post<GameSession>('/sessions', {
      module_id: moduleId,
      participants,
    })
    set((s) => ({ sessions: [session, ...s.sessions], currentSession: session }))
    return session
  },

  setCurrentSession: (session) => set({ currentSession: session }),

  addMessage: (msg) =>
    set((s) => ({
      messages: [...s.messages, { ...msg, id: msg.id || `msg-${++msgCounter}` }],
    })),

  startStreamMessage: (type, actorName) => {
    const id = `stream-${++msgCounter}`
    set((s) => ({
      messages: [...s.messages, { id, type, content: '', actor_name: actorName }],
      streamingMsgId: id,
    }))
    return id
  },

  appendToStream: (content) =>
    set((s) => {
      const sid = s.streamingMsgId
      if (!sid) return s
      return {
        messages: s.messages.map((m) =>
          m.id === sid ? { ...m, content: m.content + content } : m
        ),
      }
    }),

  endStream: () => set({ streamingMsgId: null }),

  replaceLastNarration: (content: string) =>
    set((s) => {
      const msgs = [...s.messages]
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].type === 'narration') {
          msgs[i] = { ...msgs[i], content }
          break
        }
      }
      return { messages: msgs }
    }),

  clearMessages: () => set({ messages: [], streamingMsgId: null, hasMoreHistory: false }),

  loadHistory: async (sessionId) => {
    const data = await api.get<EventsResponse>(`/sessions/${sessionId}/events`)
    const playerCharId = get().currentSession?.player_character_id ?? null
    set({
      messages: eventsToMessages(data.events, playerCharId),
      hasMoreHistory: data.has_more,
    })
  },

  loadOlderEvents: async (sessionId) => {
    const state = get()
    if (state.loadingOlder || !state.hasMoreHistory) return
    set({ loadingOlder: true })
    try {
      const firstMsg = state.messages.find((m) => m.sequence_num != null)
      const beforeSeq = firstMsg?.sequence_num
      const url = beforeSeq != null
        ? `/sessions/${sessionId}/events?before_seq=${beforeSeq}`
        : `/sessions/${sessionId}/events`
      const data = await api.get<EventsResponse>(url)
      const playerCharId = state.currentSession?.player_character_id ?? null
      const older = eventsToMessages(data.events, playerCharId)
      set((s) => ({
        messages: [...older, ...s.messages],
        hasMoreHistory: data.has_more,
        loadingOlder: false,
      }))
    } catch {
      set({ loadingOlder: false })
    }
  },
}))
