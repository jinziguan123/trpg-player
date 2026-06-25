import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, streamSSE } from '../api/client'
import { useSessionStore } from '../stores/sessionStore'
import { useModuleStore } from '../stores/moduleStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { GiReturnArrow } from 'react-icons/gi'

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

export function GamePage() {
  const {
    currentSession, messages, addMessage, clearMessages,
    createSession, setCurrentSession, loadHistory,
    fetchSessions, sessions,
    startStreamMessage, appendToStream, endStream,
  } = useSessionStore()
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const [characters, setCharacters] = useState<Character[]>([])
  const [activeChar, setActiveChar] = useState<Character | null>(null)
  const [showPanel, setShowPanel] = useState(true)
  const [moduleId, setModuleId] = useState('')
  const [charId, setCharId] = useState('')
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchModules()
    fetchSessions()
    api.get<Character[]>('/characters').then(setCharacters)
  }, [fetchModules, fetchSessions])

  useEffect(() => {
    if (currentSession?.player_character_id) {
      api.get<Character>(`/characters/${currentSession.player_character_id}`).then(setActiveChar)
    } else {
      setActiveChar(null)
    }
  }, [currentSession?.player_character_id])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const processStream = async (path: string, body?: unknown) => {
    setStreaming(true)
    let currentType = ''
    try {
      for await (const chunk of streamSSE(path, body)) {
        if (chunk.type === 'done') break

        if (chunk.type === 'dice' || chunk.type === 'system') {
          addMessage({ id: '', type: chunk.type, content: chunk.content, actor_name: chunk.actor_name, metadata: chunk.metadata })
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
    }
  }

  const startGame = async () => {
    if (!moduleId || !charId) return
    setError('')
    clearMessages()
    try {
      const session = await createSession(moduleId, charId)
      await processStream(`/sessions/${session.id}/opening`)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '创建游戏失败'
      setError(msg)
    }
  }

  const resumeGame = async (sessionId: string) => {
    const session = sessions.find((s) => s.id === sessionId)
    if (!session) return
    setCurrentSession(session)
    await loadHistory(sessionId)
  }

  const sendMessage = async () => {
    if (!input.trim() || !currentSession || streaming) return
    const text = input.trim()
    setInput('')
    addMessage({ id: '', type: 'dialogue', content: text, actor_name: '玩家' })
    await processStream(`/sessions/${currentSession.id}/chat`, { content: text })
  }

  const formatTime = (ts?: string) => {
    if (!ts) return ''
    const d = new Date(ts)
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }

  if (!currentSession) {
    const activeSessions = sessions.filter((s) => s.status === 'active' || s.status === 'paused')
    return (
      <div className="max-w-2xl mx-auto mt-8">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
            <GiReturnArrow /> 返回
          </button>
          <h2 className="page-title !mb-0">开始游戏</h2>
        </div>

        <div className="card mb-6">
          <h3 className="card-title">新游戏</h3>
          <div className="flex gap-3 mb-3">
            <select value={moduleId} onChange={(e) => setModuleId(e.target.value)} className="input flex-1">
              <option value="">— 选择模组 —</option>
              {modules.map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}
            </select>
            <select value={charId} onChange={(e) => setCharId(e.target.value)} className="input flex-1">
              <option value="">— 选择角色 —</option>
              {characters.filter((c) => !moduleId || c.module_id === moduleId).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          {error && (
            <p className="text-sm mb-2" style={{ color: 'var(--color-danger)' }}>{error}</p>
          )}
          <button onClick={startGame} disabled={!moduleId || !charId} className="btn-primary">
            开始冒险
          </button>
        </div>

        {activeSessions.length > 0 && (
          <div>
            <h3 className="card-title">继续游戏</h3>
            {activeSessions.map((s) => (
              <button
                key={s.id}
                onClick={() => resumeGame(s.id)}
                className="card w-full text-left mb-2 hover:border-[var(--color-accent)] transition-colors"
              >
                <div className="flex items-center justify-between">
                  <div>
                    <span className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>
                      {s.module_title || '未知模组'}
                    </span>
                    <span className="mx-2" style={{ color: 'var(--color-border)' }}>—</span>
                    <span>{s.character_name || '未知角色'}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      {formatTime(s.created_at)}
                    </span>
                    <span className="badge">{s.status === 'active' ? '进行中' : '已暂停'}</span>
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full gap-0">
      <div className="flex flex-col flex-1 min-w-0">
        <div className="flex items-center gap-3 pb-2 mb-2 border-b" style={{ borderColor: 'var(--color-border)' }}>
          <button
            onClick={() => { setCurrentSession(null as never); clearMessages(); setActiveChar(null) }}
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
        <div ref={scrollRef} className="flex-1 overflow-auto pb-4 chat-scroll">
          {messages.map((msg) => (
            <div key={msg.id} className={`chat-msg chat-msg--${msg.type}`}>
              {msg.actor_name && msg.actor_name !== '玩家' && msg.type === 'dialogue' && (
                <div className="chat-actor">{msg.actor_name}</div>
              )}
              {msg.type === 'dialogue' && msg.actor_name === '玩家' ? (
                <div className="chat-player">
                  <span className="chat-bubble-player">{msg.content}</span>
                </div>
              ) : (
                <div className="chat-content">
                  <span className="whitespace-pre-wrap">{msg.content}</span>
                </div>
              )}
            </div>
          ))}
          {streaming && (
            <div className="chat-loading">
              <span className="dot-pulse" />
            </div>
          )}
        </div>

        <div className="chat-input-bar">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder="输入你的行动或对话..."
            disabled={streaming}
            className="input flex-1"
          />
          <button onClick={sendMessage} disabled={streaming || !input.trim()} className="btn-primary">
            发送
          </button>
        </div>
      </div>

      {showPanel && activeChar && (
        <aside
          className="w-64 flex-shrink-0 border-l overflow-hidden"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <CharacterPanel character={activeChar} />
        </aside>
      )}
    </div>
  )
}
