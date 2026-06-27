import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, connectSSE } from '../api/client'
import type { SessionParticipant } from '../stores/sessionStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { GiReturnArrow } from 'react-icons/gi'

interface Character {
  id: string
  name: string
  base_attributes: Record<string, number>
  skills: Record<string, number>
  system_data: Record<string, unknown>
  backstory: string
  status: string
}

interface RoomData {
  id: string
  module_id: string
  status: string
  room_code?: string | null
  module_title?: string
  participants: SessionParticipant[]
}

interface ChatLine { id: string; name: string; content: string }

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Chunk = { type: string; id?: string; content?: string; actor_name?: string }

export function RoomLobbyPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [room, setRoom] = useState<RoomData | null>(null)
  const [moduleDesc, setModuleDesc] = useState('')
  const [myChars, setMyChars] = useState<Character[]>([])
  const [chat, setChat] = useState<ChatLine[]>([])
  const [chatInput, setChatInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [panelChar, setPanelChar] = useState<Character | null>(null)
  const [typingName, setTypingName] = useState('')
  const chatRef = useRef<HTMLDivElement>(null)
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastTypingSent = useRef(0)
  const myNameRef = useRef<string | null>(null)
  myNameRef.current = room?.participants.find((p) => p.is_mine)?.character_name ?? null

  const mySeat = room?.participants.find((p) => p.is_mine) ?? null
  const amHost = !!room?.participants.some((p) => p.is_host && p.is_mine)

  const gaps = useCallback((r: RoomData): string[] => {
    const out: string[] = []
    const empty = r.participants.filter((p) => !p.character_id).length
    if (empty) out.push(`还有 ${empty} 个空席未填角色`)
    const notReady = r.participants.filter((p) => p.character_id && p.role === 'human' && !p.ready).length
    if (notReady) out.push(`还有 ${notReady} 名玩家未准备`)
    return out
  }, [])

  const refreshRoom = useCallback(async () => {
    if (!sessionId) return
    const r = await api.get<RoomData>(`/sessions/${sessionId}`)
    if (r.status === 'active') { navigate(`/game/${sessionId}`, { replace: true }); return }
    setRoom(r)
  }, [sessionId, navigate])

  useEffect(() => {
    if (!sessionId) return
    const ac = new AbortController()
    let cancelled = false

    const handleChunk = (c: Chunk) => {
      if (c.type === 'started') { navigate(`/game/${sessionId}`, { replace: true }); return }
      if (c.type === 'lobby' || c.type === 'seat' || c.type === 'presence') { void refreshRoom(); return }
      if (c.type === 'typing') {
        if (c.actor_name && c.actor_name !== myNameRef.current) {
          setTypingName(c.actor_name)
          if (typingTimer.current) clearTimeout(typingTimer.current)
          typingTimer.current = setTimeout(() => setTypingName(''), 3000)
        }
        return
      }
      if (c.type === 'ooc') {
        setChat((prev) => prev.some((x) => x.id === c.id)
          ? prev
          : [...prev, { id: c.id || `${Date.now()}`, name: c.actor_name || '玩家', content: c.content || '' }])
      }
    }

    const init = async () => {
      const r = await api.get<RoomData>(`/sessions/${sessionId}`)
      if (cancelled) return
      if (r.status === 'active') { navigate(`/game/${sessionId}`, { replace: true }); return }
      setRoom(r)
      const mods = await api.get<{ id: string; description: string }[]>('/modules')
      if (cancelled) return
      setModuleDesc(mods.find((m) => m.id === r.module_id)?.description || '')
      const mine = await api.get<Character[]>('/characters?available=true&is_player=true&mine=true')
      if (cancelled) return
      setMyChars(mine)
      const ev = await api.get<{ events: { id: string; event_type: string; actor_name: string; content: string }[] }>(`/sessions/${sessionId}/events`)
      if (cancelled) return
      setChat(ev.events.filter((e) => e.event_type === 'ooc').map((e) => ({ id: e.id, name: e.actor_name, content: e.content })))

      while (!cancelled) {
        try {
          for await (const chunk of connectSSE(`/sessions/${sessionId}/live`, ac.signal)) {
            if (cancelled) break
            handleChunk(chunk as Chunk)
          }
        } catch { /* dropped */ }
        if (cancelled) break
        await new Promise((res) => setTimeout(res, 1500))
      }
    }
    init().catch(() => navigate('/game', { replace: true }))
    return () => { cancelled = true; ac.abort() }
  }, [sessionId, navigate, refreshRoom])

  useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight })
  }, [chat.length])

  const claimWithChar = async (charId: string) => {
    if (!room) return
    const seat = room.participants.find((p) => p.role === 'human' && !p.character_id && !p.claimed)
    if (!seat) { toast.error('房间已满，没有空席'); return }
    setBusy(true)
    try {
      await api.post(`/sessions/${room.id}/claim`, { seat_order: seat.seat_order, character_id: charId })
      await refreshRoom()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '入座失败')
    } finally { setBusy(false) }
  }

  const generateAndClaim = async () => {
    if (!room) return
    setBusy(true)
    try {
      const draft = await api.post<Record<string, unknown>>('/characters/ai-generate', { module_id: room.module_id, hint: '', is_player: true })
      const created = await api.post<Character>('/characters', {
        name: draft.name, module_id: room.module_id, rule_system: (draft.rule_system as string) || 'coc',
        is_player: true, age: draft.age ?? 25, base_attributes: draft.base_attributes,
        skills: draft.skills, system_data: draft.system_data, backstory: draft.backstory ?? '',
      })
      await claimWithChar(created.id)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'AI 生成角色失败')
      setBusy(false)
    }
  }

  const toggleReady = async () => {
    if (!room || !mySeat) return
    try {
      await api.post(`/sessions/${room.id}/ready`, { ready: !mySeat.ready })
      await refreshRoom()
    } catch (e) { toast.error(e instanceof Error ? e.message : '操作失败') }
  }

  const startGame = async () => {
    if (!room) return
    setBusy(true)
    try {
      await api.post(`/sessions/${room.id}/start`)
      navigate(`/game/${room.id}`, { replace: true })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '开始失败')
      setBusy(false)
    }
  }

  const sendChat = async () => {
    const text = chatInput.trim()
    if (!text || !room || !mySeat) return
    setChatInput('')
    try {
      await api.post(`/sessions/${room.id}/ooc`, { content: text, acting_character_id: mySeat.character_id })
    } catch (e) { toast.error(e instanceof Error ? e.message : '发送失败') }
  }

  const onChatInput = (v: string) => {
    setChatInput(v)
    if (!room || !mySeat) return
    const now = Date.now()
    if (now - lastTypingSent.current > 2000) {
      lastTypingSent.current = now
      api.post(`/sessions/${room.id}/typing`).catch(() => {})
    }
  }

  const kick = async (seatOrder: number) => {
    if (!room) return
    try {
      await api.post(`/sessions/${room.id}/kick/${seatOrder}`)
    } catch (e) { toast.error(e instanceof Error ? e.message : '移出失败') }
  }

  const viewSeat = async (charId: string | null) => {
    if (!charId) return
    try { setPanelChar(await api.get<Character>(`/characters/${charId}`)) } catch { /* ignore */ }
  }

  const copyCode = () => {
    if (room?.room_code) { navigator.clipboard?.writeText(room.room_code); toast.success('房间码已复制') }
  }

  if (!room) {
    return <div className="flex items-center justify-center h-full text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载房间中…</div>
  }

  const seatGaps = gaps(room)
  const seats = [...room.participants].sort((a, b) => a.seat_order - b.seat_order)

  const seatIcon = (p: SessionParticipant) => {
    if (!p.character_id) return '🪑'
    if (p.role === 'ai') return '🤖'
    if (p.is_host) return '👑'
    if (p.is_mine) return '🙋'
    return '👤'
  }

  return (
    <div className="flex h-full gap-0">
      <div className="flex flex-col flex-1 min-w-0 max-w-3xl mx-auto w-full">
        <div className="flex items-center gap-3 pb-2 mb-3 border-b" style={{ borderColor: 'var(--color-border)' }}>
          <button onClick={() => navigate('/game')} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
            <GiReturnArrow /> 返回
          </button>
          <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>
            房间大厅 · {room.module_title || '未知模组'}
          </span>
          {room.room_code && (
            <button onClick={copyCode} className="ml-auto badge" title="点击复制房间码">
              房间码 {room.room_code} ⧉
            </button>
          )}
        </div>

        {moduleDesc && (
          <div className="card mb-3">
            <h3 className="card-title">模组简介</h3>
            <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>{moduleDesc}</p>
          </div>
        )}

        {/* 席位 */}
        <div className="card mb-3">
          <h3 className="card-title">席位（{seats.filter((s) => s.character_id).length}/{seats.length}）</h3>
          <div className="space-y-1.5">
            {seats.map((p) => {
              const readyBadge = p.role === 'human' && p.character_id
                ? (p.ready ? '✓ 已准备' : '… 待准备')
                : (p.character_id ? '就绪' : '')
              const showDot = p.role === 'human' && p.character_id
              const canKick = amHost && p.character_id && p.role === 'human' && !p.is_primary
              return (
                <div key={p.seat_order} className="flex items-center gap-2 px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <span style={{ fontSize: '0.8rem' }}>{seatIcon(p)}</span>
                  {showDot && (
                    <span
                      title={p.is_online ? '在线' : '离线'}
                      style={{ width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                        background: p.is_online ? 'var(--color-success)' : 'var(--color-border)' }}
                    />
                  )}
                  <button
                    onClick={() => viewSeat(p.character_id)}
                    disabled={!p.character_id}
                    className="text-sm text-left flex-1 disabled:cursor-default"
                    style={{ color: p.is_mine ? 'var(--color-text-accent)' : 'var(--color-text-primary)' }}
                  >
                    {p.character_id ? p.character_name || '未知角色' : '空席 · 等待真人加入'}
                    {p.is_host ? ' · 房主' : ''}{p.is_mine ? '（我）' : ''}
                  </button>
                  {readyBadge && (
                    <span className="text-xs" style={{ color: p.ready || p.role !== 'human' ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>
                      {readyBadge}
                    </span>
                  )}
                  {canKick && (
                    <button
                      onClick={() => kick(p.seat_order)}
                      className="text-xs px-1.5 py-0.5 rounded"
                      style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                      title="移出该玩家（席位回到空席）"
                    >移出</button>
                  )}
                </div>
              )
            })}
          </div>

          {/* 我的操作区 */}
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--color-border)' }}>
            {!mySeat ? (
              <div>
                <p className="text-sm mb-2" style={{ color: 'var(--color-text-secondary)' }}>选择你的角色入座空席：</p>
                <div className="flex flex-wrap gap-2">
                  {myChars.map((c) => (
                    <button key={c.id} onClick={() => claimWithChar(c.id)} disabled={busy}
                      className="px-2.5 py-1 rounded-full text-xs border"
                      style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}>
                      {c.name}
                    </button>
                  ))}
                  <button onClick={generateAndClaim} disabled={busy} className="btn-secondary !px-2 !py-1 text-xs">
                    {busy ? '处理中…' : '✨ AI 生成角色并入座'}
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-3">
                <button onClick={toggleReady} className={mySeat.ready ? 'btn-secondary' : 'btn-primary'}>
                  {mySeat.ready ? '取消准备' : '准备'}
                </button>
                <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                  你已入座为「{mySeat.character_name}」
                </span>
              </div>
            )}
          </div>
        </div>

        {/* 大厅聊天 */}
        <div className="card mb-3 flex flex-col" style={{ minHeight: 160 }}>
          <h3 className="card-title">大厅聊天</h3>
          <div ref={chatRef} className="flex-1 overflow-auto mb-2 space-y-1" style={{ maxHeight: 200 }}>
            {chat.length === 0 ? (
              <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>还没有人发言。开局前可以在这里商量。</p>
            ) : chat.map((m) => (
              <div key={m.id} className="text-sm">
                <span className="font-semibold" style={{ color: 'var(--color-text-accent)' }}>{m.name}：</span>
                <span>{m.content}</span>
              </div>
            ))}
          </div>
          <div className="h-4 text-xs italic mb-0.5" style={{ color: 'var(--color-text-secondary)' }}>
            {typingName ? `${typingName} 正在输入…` : ''}
          </div>
          <div className="flex gap-2">
            <input
              value={chatInput}
              onChange={(e) => onChatInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') sendChat() }}
              placeholder={mySeat ? '说点什么…' : '入座后可发言'}
              disabled={!mySeat}
              className="input flex-1"
            />
            <button onClick={sendChat} disabled={!mySeat || !chatInput.trim()} className="btn-primary">发送</button>
          </div>
        </div>

        {/* 开局 */}
        <div className="card mb-6">
          {amHost ? (
            <div className="flex items-center gap-3">
              <button onClick={startGame} disabled={busy || seatGaps.length > 0} className="btn-primary">
                开始游戏
              </button>
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {seatGaps.length > 0 ? seatGaps.join('；') : '所有人就绪，可以开始'}
              </span>
            </div>
          ) : (
            <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {seatGaps.length > 0 ? `等待中：${seatGaps.join('；')}` : '所有人就绪，等待房主开始…'}
            </p>
          )}
        </div>
      </div>

      {panelChar && (
        <aside className="w-64 flex-shrink-0 border-l overflow-y-auto" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-card)' }}>
          <div className="flex items-center justify-between px-3 py-1.5 text-xs border-b" style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}>
            <span>角色卡</span>
            <button onClick={() => setPanelChar(null)} className="btn-secondary !px-2 !py-0.5">关闭</button>
          </div>
          <CharacterPanel character={panelChar} />
        </aside>
      )}
    </div>
  )
}
