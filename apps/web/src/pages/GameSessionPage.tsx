import { useEffect, useState, useRef, useCallback } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, streamSSE, connectSSE } from '../api/client'
import { useSessionStore } from '../stores/sessionStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { PartyRoster } from '../components/game/PartyRoster'
import { GiReturnArrow } from 'react-icons/gi'

const CMD_TAG_RE = /\[(DICE_CHECK|NPC_ACT|SCENE_CHANGE):[^\]]*\]/g

function stripCommandTags(text: string): string {
  return text.replace(CMD_TAG_RE, '').replace(/\n{3,}/g, '\n\n').trim()
}

interface Character {
  id: string
  name: string
  module_id: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ChunkIterator = AsyncGenerator<any, void, unknown>

export function GameSessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const isNew = (location.state as { isNew?: boolean })?.isNew
  const {
    currentSession, messages, addMessage, clearMessages,
    setCurrentSession, loadHistory, loadOlderEvents,
    hasMoreHistory, loadingOlder,
    startStreamMessage, appendToStream, endStream,
    replaceLastNarration, fetchSessions, sessions,
  } = useSessionStore()

  const [activeChar, setActiveChar] = useState<Character | null>(null)
  const [showPanel, setShowPanel] = useState(true)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const openingTriggered = useRef(false)
  const initedSessionId = useRef<string | null>(null)
  const composingRef = useRef(false)

  const consumeStream = useCallback(async (iter: ChunkIterator) => {
    setStreaming(true)
    let currentType = ''
    try {
      for await (const chunk of iter) {
        if (chunk.type === 'done') break
        if (chunk.type === 'narration_replace') {
          endStream()
          replaceLastNarration(stripCommandTags(chunk.content))
          currentType = ''
          continue
        }
        if (chunk.type === 'dice' || chunk.type === 'system' || chunk.type === 'npc_dialogue') {
          endStream()
          const msgType = chunk.type === 'npc_dialogue' ? 'dialogue' : chunk.type
          addMessage({ id: '', type: msgType, content: chunk.content, actor_name: chunk.actor_name, metadata: chunk.metadata })
          currentType = ''
          continue
        }
        if (chunk.type !== currentType) {
          endStream()
          startStreamMessage(chunk.type, chunk.actor_name)
          currentType = chunk.type
        }
        appendToStream(chunk.content)
      }
      endStream()
    } finally {
      setStreaming(false)
      if (currentSession?.player_character_id) {
        api.get<Character>(`/characters/${currentSession.player_character_id}`).then(setActiveChar)
      }
    }
  }, [addMessage, appendToStream, endStream, replaceLastNarration, startStreamMessage, currentSession?.player_character_id])

  useEffect(() => {
    if (!sessionId) return
    // 用 sessionId 作为守卫键：同一 session 只初始化一次。
    // 关键是不在 cleanup 中重置它——React Strict Mode 的 mount→unmount→mount
    // 会复用同一组件实例（ref 保持），第二次 mount 因守卫命中而跳过，
    // 避免 opening 被同时直连 + 订阅造成内容重复。真正离开路由时组件实例
    // 销毁、ref 归零，下次进入自然重新加载。
    if (initedSessionId.current === sessionId) return
    initedSessionId.current = sessionId
    clearMessages()
    const init = async () => {
      let list = useSessionStore.getState().sessions
      if (list.length === 0) {
        await fetchSessions()
        list = useSessionStore.getState().sessions
      }
      const session = list.find((s) => s.id === sessionId)
      if (!session) {
        navigate('/game', { replace: true })
        return
      }
      setCurrentSession(session)

      if (isNew && !openingTriggered.current) {
        openingTriggered.current = true
        await consumeStream(streamSSE(`/sessions/${sessionId}/opening`))
      } else {
        await loadHistory(sessionId)
        const { generating } = await api.get<{ generating: boolean }>(`/sessions/${sessionId}/generating`)
        if (generating) {
          await consumeStream(connectSSE(`/sessions/${sessionId}/stream`))
        }
      }
    }
    init()
  }, [sessionId])

  useEffect(() => {
    if (currentSession?.player_character_id) {
      api.get<Character>(`/characters/${currentSession.player_character_id}`).then(setActiveChar)
    } else {
      setActiveChar(null)
    }
  }, [currentSession?.player_character_id])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages.length])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el || !sessionId || loadingOlder || !hasMoreHistory) return
    if (el.scrollTop < 80) {
      const prevHeight = el.scrollHeight
      loadOlderEvents(sessionId).then(() => {
        requestAnimationFrame(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight - prevHeight
          }
        })
      })
    }
  }, [sessionId, loadingOlder, hasMoreHistory, loadOlderEvents])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.addEventListener('scroll', handleScroll)
    return () => el.removeEventListener('scroll', handleScroll)
  }, [handleScroll])

  const sendMessage = async () => {
    if (!input.trim() || !currentSession || streaming) return
    const text = input.trim()
    setInput('')
    if (inputRef.current) inputRef.current.style.height = 'auto'
    addMessage({ id: '', type: 'dialogue', content: text, actor_name: activeChar?.name || '玩家', metadata: { is_player: true } })
    await consumeStream(streamSSE(`/sessions/${currentSession.id}/chat`, { content: text }))
  }

  if (!currentSession) {
    return <div className="flex items-center justify-center h-full text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载中...</div>
  }

  return (
    <div className="flex h-full gap-0">
      <div className="flex flex-col flex-1 min-w-0">
        <div className="flex items-center gap-3 pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
          <button
            onClick={() => navigate('/game')}
            className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm"
          >
            <GiReturnArrow /> 返回列表
          </button>
          <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>
            {sessions.find((s) => s.id === currentSession.id)?.module_title || '游戏中'}
          </span>
          <button
            onClick={() => setShowPanel(!showPanel)}
            className="ml-auto text-xs btn-secondary !px-2 !py-0.5"
          >
            {showPanel ? '收起角色卡' : '展开角色卡'}
          </button>
        </div>
        {currentSession.participants && currentSession.participants.length > 1 && (
          <div className="pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
            <PartyRoster participants={currentSession.participants} />
          </div>
        )}
        <div ref={scrollRef} className="flex-1 overflow-auto pb-4 chat-scroll">
          {loadingOlder && (
            <div className="text-center py-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              加载更早的记录...
            </div>
          )}
          {messages.map((msg) => {
            const isPlayer = !!msg.metadata?.is_player
            const showLabel = msg.actor_name && (msg.type === 'dialogue' || msg.type === 'action')
            return (
              <div key={msg.id} className={`chat-msg chat-msg--${msg.type}`}>
                {showLabel && (
                  <div className={isPlayer ? 'chat-actor-player' : 'chat-actor'}>{msg.actor_name}</div>
                )}
                {isPlayer && msg.type === 'dialogue' ? (
                  <div className="chat-player">
                    <span className="chat-bubble-player">{msg.content}</span>
                  </div>
                ) : !isPlayer && msg.type === 'dialogue' ? (
                  <div>
                    <span className="chat-bubble-npc">{msg.content}</span>
                  </div>
                ) : msg.type === 'narration' ? (
                  <div className="chat-content markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {stripCommandTags(msg.content)}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="chat-content">
                    <span className="whitespace-pre-wrap">{msg.content}</span>
                  </div>
                )}
              </div>
            )
          })}
          {streaming && (
            <div className="chat-loading">
              <span className="dot-pulse" />
            </div>
          )}
        </div>

        <div className="chat-input-bar">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
            }}
            onCompositionStart={() => { composingRef.current = true }}
            onCompositionEnd={() => { composingRef.current = false }}
            onKeyDown={(e) => {
              if (composingRef.current) return
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                sendMessage()
              }
            }}
            placeholder="输入你的行动或对话..."
            disabled={streaming}
            className="input flex-1"
            rows={1}
            style={{ resize: 'none' }}
          />
          <button onClick={sendMessage} disabled={streaming || !input.trim()} className="btn-primary">
            发送
          </button>
        </div>
      </div>

      {showPanel && activeChar && (
        <aside
          className="w-64 flex-shrink-0 border-l overflow-y-auto"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <CharacterPanel character={activeChar} />
        </aside>
      )}
    </div>
  )
}
