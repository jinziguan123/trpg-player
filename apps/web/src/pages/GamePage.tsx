import { useEffect, useState, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api, streamSSE } from '../api/client'
import { useSessionStore } from '../stores/sessionStore'
import { useModuleStore } from '../stores/moduleStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
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

export function GamePage() {
  const {
    currentSession, messages, addMessage, clearMessages,
    createSession, setCurrentSession, resumeSession,
    fetchSessions, sessions,
    startStreamMessage, appendToStream, endStream,
    replaceLastNarration,
  } = useSessionStore()
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const { sessionId } = useParams<{ sessionId?: string }>()
  const [characters, setCharacters] = useState<Character[]>([])
  const [activeChar, setActiveChar] = useState<Character | null>(null)
  const [showPanel, setShowPanel] = useState(true)
  const [moduleId, setModuleId] = useState('')
  const [charId, setCharId] = useState('')
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    fetchModules()
    fetchSessions()
    api.get<Character[]>('/characters?available=true').then(setCharacters)
  }, [fetchModules, fetchSessions])

  // URL 即会话的真实来源：刷新后据 sessionId 恢复会话并加载历史记录
  useEffect(() => {
    if (!sessionId) {
      setCurrentSession(null as never)
      clearMessages()
      return
    }
    if (useSessionStore.getState().currentSession?.id === sessionId) return
    resumeSession(sessionId).catch(() => navigate('/game', { replace: true }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  }, [messages])

  const processStream = async (path: string, body?: unknown) => {
    setStreaming(true)
    let currentType = ''
    try {
      for await (const chunk of streamSSE(path, body)) {
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
    }
  }

  const startGame = async () => {
    if (!moduleId || !charId) return
    setError('')
    clearMessages()
    try {
      const session = await createSession(moduleId, charId)
      navigate(`/game/${session.id}`)
      await processStream(`/sessions/${session.id}/opening`)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '创建游戏失败'
      setError(msg)
    }
  }

  const resumeGame = (sessionId: string) => {
    navigate(`/game/${sessionId}`)
  }

  const deleteSession = async (sessionId: string) => {
    try {
      await api.delete(`/sessions/${sessionId}`)
      await fetchSessions()
      await api.get<Character[]>('/characters?available=true').then(setCharacters)
      toast.success('游戏存档已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const sendMessage = async () => {
    if (!input.trim() || !currentSession || streaming) return
    const text = input.trim()
    setInput('')
    if (inputRef.current) inputRef.current.style.height = 'auto'
    addMessage({ id: '', type: 'dialogue', content: text, actor_name: activeChar?.name || '玩家', metadata: { is_player: true } })
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
            <Select value={moduleId} onValueChange={(v) => { setModuleId(v); setCharId('') }}>
              <SelectTrigger className="flex-1">
                <SelectValue placeholder="— 选择模组 —" />
              </SelectTrigger>
              <SelectContent>
                {modules.map((m) => <SelectItem key={m.id} value={m.id}>{m.title}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={charId} onValueChange={setCharId}>
              <SelectTrigger className="flex-1">
                <SelectValue placeholder="— 选择角色 —" />
              </SelectTrigger>
              <SelectContent>
                {characters.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
              </SelectContent>
            </Select>
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
              <div
                key={s.id}
                onClick={() => resumeGame(s.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === 'Enter') resumeGame(s.id) }}
                className="card w-full text-left mb-2 hover:border-[var(--color-accent)] transition-colors cursor-pointer"
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
                    <ConfirmDialog
                      title="删除游戏"
                      description="确定要删除该游戏存档吗？聊天记录将一并删除，此操作不可恢复。"
                      confirmLabel="删除"
                      onConfirm={() => deleteSession(s.id)}
                    >
                      {(open) => (
                        <button
                          onClick={(e) => { e.stopPropagation(); open() }}
                          className="text-xs px-1.5 py-0.5 rounded hover:bg-[var(--color-danger)] hover:text-white transition-colors"
                          style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                        >
                          删除
                        </button>
                      )}
                    </ConfirmDialog>
                  </div>
                </div>
              </div>
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
        <div ref={scrollRef} className="flex-1 overflow-auto pb-4 chat-scroll">
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
                ) : (
                  <div className="chat-content">
                    <span className="whitespace-pre-wrap">
                      {msg.type === 'narration' ? stripCommandTags(msg.content) : msg.content}
                    </span>
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
            onKeyDown={(e) => {
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
          className="w-64 flex-shrink-0 border-l overflow-hidden"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}
        >
          <CharacterPanel character={activeChar} />
        </aside>
      )}
    </div>
  )
}
