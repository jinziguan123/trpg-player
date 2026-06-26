import { create } from 'zustand'
import { api } from '../api/client'

interface GameSession {
  id: string
  module_id: string
  status: string
  player_character_id: string | null
  current_scene_id: string | null
  world_state: Record<string, unknown>
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
}

interface SessionStore {
  sessions: GameSession[]
  currentSession: GameSession | null
  messages: ChatMessage[]
  loading: boolean
  streamingMsgId: string | null
  fetchSessions: () => Promise<void>
  createSession: (moduleId: string, characterId: string) => Promise<GameSession>
  setCurrentSession: (session: GameSession) => void
  addMessage: (msg: ChatMessage) => void
  startStreamMessage: (type: string, actorName?: string) => string
  appendToStream: (content: string) => void
  endStream: () => void
  replaceLastNarration: (content: string) => void
  clearMessages: () => void
  loadHistory: (sessionId: string) => Promise<void>
}

let msgCounter = 0

export const useSessionStore = create<SessionStore>((set, get) => ({
  sessions: [],
  currentSession: null,
  messages: [],
  loading: false,
  streamingMsgId: null,

  fetchSessions: async () => {
    const sessions = await api.get<GameSession[]>('/sessions')
    set({ sessions })
  },

  createSession: async (moduleId, characterId) => {
    const session = await api.post<GameSession>('/sessions', {
      module_id: moduleId,
      player_character_id: characterId,
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

  clearMessages: () => set({ messages: [], streamingMsgId: null }),

  loadHistory: async (sessionId) => {
    const events = await api.get<Array<{
      id: string
      event_type: string
      actor_id: string | null
      actor_name: string
      content: string
      metadata_: Record<string, unknown>
    }>>(`/sessions/${sessionId}/events`)

    const state = get()
    const playerCharId = state.currentSession?.player_character_id
    const messages: ChatMessage[] = events.map((e) => ({
      id: e.id,
      type: e.event_type,
      content: e.content,
      actor_name: e.actor_name,
      metadata: { ...e.metadata_, is_player: !!(playerCharId && e.actor_id === playerCharId) },
    }))
    set({ messages })
  },
}))
