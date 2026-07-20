import { create } from 'zustand'
import { api } from '../api/client'

export interface SessionParticipant {
  character_id: string | null
  role: string // human | ai | kp
  is_primary: boolean
  seat_order: number
  claimed: boolean
  ready: boolean
  is_mine: boolean
  is_host: boolean
  is_online: boolean
  is_kp?: boolean
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
  kp_mode?: 'ai' | 'human'
  identity_version?: number
  player_character_id: string | null
  room_code?: string | null
  current_scene_id: string | null
  world_state: Record<string, unknown>
  participants?: SessionParticipant[]
  module_title?: string
  character_name?: string
  created_at?: string
}

export interface ChatMessage {
  id: string
  type: string
  content: string
  actor_name?: string
  metadata?: Record<string, unknown>
  sequence_num?: number
  ts?: number
}

interface EventPayload {
  id: string
  sequence_num: number
  event_type: string
  actor_id: string | null
  actor_name: string
  content: string
  metadata_: Record<string, unknown>
  created_at?: string
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
  createSession: (moduleId: string, participants: ParticipantInput[], kpMode?: 'ai' | 'human') => Promise<GameSession>
  setCurrentSession: (session: GameSession) => void
  addMessage: (msg: ChatMessage) => void
  removeMessage: (id: string) => void
  updateMessage: (id: string, content: string) => void
  patchMessageMetadata: (id: string, patch: Record<string, unknown>) => void
  startStreamMessage: (type: string, actorName?: string, metadata?: Record<string, unknown>) => string
  appendToStream: (content: string) => void
  endStream: () => void
  replaceLastNarration: (content: string) => void
  clearMessages: () => void
  loadHistory: (sessionId: string) => Promise<void>
  loadOlderEvents: (sessionId: string) => Promise<void>
}

let msgCounter = 0

/** 后端 created_at 是 naive UTC（SQLite CURRENT_TIMESTAMP / func.now()，无时区后缀）。
 *  若不含时区信息，按 UTC 解析，否则会被 JS 当成本地时间、少算时区偏移（如东八区差 8 小时）。*/
function parseServerTime(s?: string): number | undefined {
  if (!s) return undefined
  let v = s.includes('T') ? s : s.replace(' ', 'T')
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(v)) v += 'Z'
  const t = new Date(v).getTime()
  return Number.isNaN(t) ? undefined : t
}

function eventsToMessages(events: EventPayload[], playerCharId: string | null): ChatMessage[] {
  return events.map((e) => ({
    id: e.id,
    type: e.event_type,
    content: e.content,
    actor_name: e.actor_name,
    sequence_num: e.sequence_num,
    ts: parseServerTime(e.created_at),
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

  createSession: async (moduleId, participants, kpMode = 'ai') => {
    const session = await api.post<GameSession>('/sessions', {
      module_id: moduleId,
      participants,
      kp_mode: kpMode,
    })
    set((s) => ({ sessions: [session, ...s.sessions], currentSession: session }))
    return session
  },

  setCurrentSession: (session) => set({ currentSession: session }),

  addMessage: (msg) =>
    set((s) => {
      const id = msg.id || `msg-${++msgCounter}`
      // 按 id 幂等：同一事件可能被广播两次（如战斗骰为降低延迟先由后端即时广播、随后端点又随
      // 整批重发一次）——去重避免重复卡片，首条已触发 3D 动画即可。
      if (s.messages.some((m) => m.id === id)) return s
      return { messages: [...s.messages, { ts: Date.now(), ...msg, id }] }
    }),

  removeMessage: (id) =>
    set((s) => ({ messages: s.messages.filter((m) => m.id !== id) })),

  updateMessage: (id, content) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, content } : m)) })),

  /** 事件 metadata 增量更新（SSE event_patch，如手书配图异步生成完成后补 image）：
   *  按事件 id 找到已渲染消息，浅合并 patch；找不到（如尚未拉到历史）则静默忽略。 */
  patchMessageMetadata: (id, patch) =>
    set((s) => {
      if (!s.messages.some((m) => m.id === id)) return s
      return {
        messages: s.messages.map((m) =>
          m.id === id ? { ...m, metadata: { ...(m.metadata || {}), ...patch } } : m
        ),
      }
    }),

  startStreamMessage: (type, actorName, metadata) => {
    const id = `stream-${++msgCounter}`
    set((s) => ({
      messages: [...s.messages, { id, type, content: '', actor_name: actorName, metadata }],
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
