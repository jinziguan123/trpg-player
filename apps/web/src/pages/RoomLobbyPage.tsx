import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, connectSSE, getServerUrl, localApi } from '../api/client'
import type { SessionParticipant } from '../stores/sessionStore'
import { CharacterPanel } from '../components/character/CharacterPanel'
import { SeatIcon, seatKind } from '../components/game/SeatIcon'
import { GiReturnArrow } from 'react-icons/gi'
import { Copy, Sparkles, Check, Eye, ScanSearch } from 'lucide-react'

interface Character {
  id: string
  name: string
  module_id?: string | null
  rule_system?: string
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
  kp_mode?: 'ai' | 'human'
  identity_version?: number
  participants: SessionParticipant[]
}

interface ChatLine { id: string; name: string; content: string }
interface CharacterEvaluation {
  compatible: boolean
  warnings: string[]
  suggestions: string[]
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Chunk = { type: string; id?: string; content?: string; actor_name?: string }

export function RoomLobbyPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [room, setRoom] = useState<RoomData | null>(null)
  const [moduleDesc, setModuleDesc] = useState('')
  const [myChars, setMyChars] = useState<Character[]>([])
  const [localChars, setLocalChars] = useState<Character[]>([])
  const [chat, setChat] = useState<ChatLine[]>([])
  const [chatInput, setChatInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [panelChar, setPanelChar] = useState<Character | null>(null)
  const [evaluation, setEvaluation] = useState<CharacterEvaluation | null>(null)
  const [evaluating, setEvaluating] = useState(false)
  const [typingName, setTypingName] = useState('')
  const chatRef = useRef<HTMLDivElement>(null)
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastTypingSent = useRef(0)
  const myNameRef = useRef<string | null>(null)
  const myPlayerSeat = room?.participants.find((p) => p.is_mine && p.role !== 'kp') ?? null
  const myKpSeat = room?.participants.find((p) => p.is_mine && p.role === 'kp') ?? null
  myNameRef.current = myPlayerSeat?.character_name ?? null

  const mySeat = myPlayerSeat
  const needsCharacter = !!mySeat && mySeat.role === 'human' && !mySeat.character_id
  // 旧 human-KP 房间可能让同一 token 同时拥有 KP/玩家席，保留玩家操作区，避免升级后失去权限。
  const strictKpIdentity = !!myKpSeat && (room?.identity_version ?? 1) >= 2
  const amHost = !!room?.participants.some((p) => p.is_host && p.is_mine)

  const gaps = useCallback((r: RoomData): string[] => {
    const out: string[] = []
    const playerSeats = r.participants.filter((p) => p.role !== 'kp')
    const empty = r.kp_mode === 'human' && r.identity_version && r.identity_version >= 2
      ? playerSeats.filter((p) => p.role === 'ai' && !p.character_id).length
      : playerSeats.filter((p) => !p.character_id).length
    if (empty) out.push(`还有 ${empty} 个空席未填角色`)
    const notReady = playerSeats.filter((p) => p.character_id && p.role === 'human' && !p.ready).length
    if (notReady) out.push(`还有 ${notReady} 名玩家未准备`)
    if (!playerSeats.some((p) => p.role === 'human' && p.character_id)) out.push('至少需要 1 名真人玩家')
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
      // 通过房间码进入后，先把 token 写入一个真人席；这样切换页面仍能在游戏列表中找到房间。
      // 若 KP 席也空着，先让用户选择加入身份；否则默认按真人玩家预留席位。
      const hasOpenKpSeat = r.participants.some((p) => p.role === 'kp' && !p.claimed)
      const joined = r.status === 'setup' && !hasOpenKpSeat
        ? await api.post<RoomData>(`/sessions/${sessionId}/join`)
        : r
      if (cancelled) return
      setRoom(joined)
      const mods = await api.get<{ id: string; description: string }[]>('/modules')
      if (cancelled) return
      setModuleDesc(mods.find((m) => m.id === joined.module_id)?.description || '')
      const mine = await api.get<Character[]>('/characters?available=true&is_player=true&mine=true')
      if (cancelled) return
      setMyChars(mine)
      if (getServerUrl()) {
        try {
          const local = await localApi.get<Character[]>('/characters?available=true&is_player=true&mine=true')
          if (!cancelled) setLocalChars(local)
        } catch {
          // 本机后端不可用时仍可使用房主主机上的角色。
        }
      }
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

  const claimWithChar = async (charId: string, notify = true): Promise<string | null> => {
    if (!room) return '房间状态尚未加载'
    const seat = (mySeat && mySeat.role === 'human' && !mySeat.character_id)
      ? mySeat
      : room.participants.find((p) => p.role === 'human' && !p.character_id && !p.claimed)
    if (!seat) {
      const message = '房间已满，没有空席'
      if (notify) toast.error(message)
      setBusy(false)
      return message
    }
    setBusy(true)
    try {
      await api.post(`/sessions/${room.id}/claim`, { seat_order: seat.seat_order, character_id: charId })
      await refreshRoom()
      return null
    } catch (e) {
      const message = e instanceof Error ? e.message : '入座失败'
      if (notify) toast.error(message)
      return message
    } finally { setBusy(false) }
  }

  const importAndClaim = async (character: Character) => {
    if (!room) return
    setBusy(true)
    try {
      const imported = await api.post<Character>('/characters', {
        name: character.name,
        module_id: room.module_id,
        rule_system: character.rule_system || 'coc',
        is_player: true,
        base_attributes: character.base_attributes,
        skills: character.skills,
        system_data: character.system_data,
        backstory: character.backstory,
      })
      const claimError = await claimWithChar(imported.id, false)
      if (claimError) {
        setMyChars((prev) => prev.some((c) => c.id === imported.id) ? prev : [...prev, imported])
        toast.error(`角色已导入房主主机，但入座失败：${claimError}。可从房主角色列表重试`)
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '导入角色失败')
    } finally { setBusy(false) }
  }

  const claimKp = async () => {
    if (!room) return
    const seat = room.participants.find((p) => p.role === 'kp' && !p.claimed)
    if (!seat) { toast.error('没有空的 KP 席位'); return }
    setBusy(true)
    try {
      await api.post(`/sessions/${room.id}/claim`, { seat_order: seat.seat_order, character_id: null })
      await refreshRoom()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '加入 KP 席失败')
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
      const claimError = await claimWithChar(created.id, false)
      if (claimError) {
        setMyChars((prev) => prev.some((c) => c.id === created.id) ? prev : [...prev, created])
        toast.error(`角色已生成，但入座失败：${claimError}。可从角色列表重试`)
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'AI 生成角色失败')
    } finally { setBusy(false) }
  }

  const toggleReady = async () => {
    if (!room || !mySeat?.character_id) return
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
    if (!text || !room || !mySeat?.character_id) return
    setChatInput('')
    try {
      await api.post(`/sessions/${room.id}/ooc`, { content: text, acting_character_id: mySeat.character_id })
    } catch (e) { toast.error(e instanceof Error ? e.message : '发送失败') }
  }

  const onChatInput = (v: string) => {
    setChatInput(v)
    if (!room || !mySeat?.character_id) return
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
    try {
      setEvaluation(null)
      setPanelChar(await api.get<Character>(`/characters/${charId}`))
    } catch { /* ignore */ }
  }

  const evaluateSeat = async (charId: string | null) => {
    if (!charId || !room) return
    setEvaluating(true)
    try {
      const character = await api.get<Character>(`/characters/${charId}`)
      setPanelChar(character)
      const systemData = character.system_data || {}
      const result = await api.post<CharacterEvaluation>('/characters/evaluate', {
        module_id: room.module_id,
        name: character.name,
        occupation: String(systemData.occupation || systemData.profession || ''),
        backstory: character.backstory,
      })
      setEvaluation(result)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '角色适配评估失败')
    } finally { setEvaluating(false) }
  }

  const copyCode = () => {
    if (room?.room_code) { navigator.clipboard?.writeText(room.room_code); toast.success('房间码已复制') }
  }

  if (!room) {
    return <div className="flex items-center justify-center h-full text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载房间中…</div>
  }

  const seatGaps = gaps(room)
  const seats = room.participants.filter((p) => p.role !== 'kp').sort((a, b) => a.seat_order - b.seat_order)
  const kpSeats = room.participants.filter((p) => p.role === 'kp').sort((a, b) => a.seat_order - b.seat_order)
  const openKpSeat = kpSeats.find((p) => !p.claimed)

  return (
    <div className="flex h-full gap-0">
      <div className="flex flex-col flex-1 min-w-0 max-w-3xl mx-auto w-full">
        <div className="flex items-center gap-3 pb-2 mb-3 border-b" style={{ borderColor: 'var(--color-border)' }}>
          <button onClick={() => navigate('/game')} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
            <GiReturnArrow /> 返回
          </button>
          <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>
            房间大厅 · {room.module_title || '未知模组'}
            {room.kp_mode === 'human' && <span className="badge ml-2 text-xs">真人 KP</span>}
          </span>
          {room.room_code && (
            <button onClick={copyCode} className="ml-auto badge inline-flex items-center gap-1" title="点击复制房间码">
              房间码 {room.room_code} <Copy size={11} />
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
          <h3 className="card-title">玩家席位（{seats.filter((s) => s.character_id).length}/{seats.length}）</h3>
          {kpSeats.length > 0 && (
            <div className="mb-2 space-y-1.5">
              {kpSeats.map((p) => (
                <div key={p.seat_order} className="flex items-center gap-2 px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <SeatIcon kind={p.is_host ? 'host' : 'human'} size={15} />
                  <span className="text-sm flex-1" style={{ color: p.is_mine ? 'var(--color-text-accent)' : 'var(--color-text-primary)' }}>
                    {p.claimed ? '真人 KP' : '空席 · 等待真人 KP 加入'}
                    {p.is_host ? ' · 房主' : ''}{p.is_mine ? '（我）' : ''}
                  </span>
                  {p.claimed && <span className="text-xs" style={{ color: 'var(--color-success)' }}>已加入</span>}
                </div>
              ))}
            </div>
          )}
          <div className="space-y-1.5">
            {seats.map((p) => {
              const readyBadge = p.role === 'human' && p.character_id
                ? (p.ready ? '已准备' : '待准备')
                : (p.character_id ? '就绪' : '')
              const showDot = p.role === 'human' && p.character_id
              const canKick = amHost && p.claimed && p.role === 'human' && !p.is_primary && !p.is_mine
              const seatLabel = p.character_id
                ? p.character_name || '未知角色'
                : p.claimed
                  ? (p.is_mine ? '已加入，等待选择角色' : '已预留 · 等待玩家选择角色')
                  : '空席 · 等待真人加入'
              return (
                <div key={p.seat_order} className="flex items-center gap-2 px-2 py-1.5 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
                  <SeatIcon kind={seatKind(p)} size={15} />
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
                    {seatLabel}
                    {p.is_host ? ' · 房主' : ''}{p.is_mine ? '（我）' : ''}
                  </button>
                  {p.character_id && (
                    <button
                      onClick={() => viewSeat(p.character_id)}
                      className="btn-secondary !px-1.5 !py-1"
                      title="查看角色卡"
                      aria-label="查看角色卡"
                    >
                      <Eye size={13} />
                    </button>
                  )}
                  {p.character_id && p.role === 'human' && (myKpSeat || (room.kp_mode === 'human' && amHost)) && (
                    <button
                      onClick={() => void evaluateSeat(p.character_id)}
                      disabled={evaluating}
                      className="btn-secondary !px-1.5 !py-1"
                      title="评估角色是否适合本模组"
                      aria-label="评估角色适配性"
                    >
                      <ScanSearch size={13} />
                    </button>
                  )}
                  {readyBadge && (
                    <span className="text-xs inline-flex items-center gap-0.5" style={{ color: p.ready || p.role !== 'human' ? 'var(--color-success)' : 'var(--color-text-secondary)' }}>
                      {(p.ready || p.role !== 'human') && <Check size={12} />}{readyBadge}
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
            {strictKpIdentity ? (
              <div className="flex items-center gap-3">
                <span className="text-sm" style={{ color: 'var(--color-text-accent)' }}>你已作为真人 KP 加入</span>
                {myKpSeat.is_host && <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>房主权限独立于玩家席</span>}
              </div>
            ) : !mySeat || needsCharacter ? (
              <div>
                {openKpSeat && (
                  <div className="mb-3 flex items-center gap-2">
                    <button onClick={claimKp} disabled={busy} className="btn-primary !px-2.5 !py-1 text-sm">
                      以真人 KP 身份加入
                    </button>
                    <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>占用 KP 席后，本 token 不能再占玩家席</span>
                  </div>
                )}
                <p className="text-sm mb-2" style={{ color: 'var(--color-text-secondary)' }}>或选择玩家角色入座空席：</p>
                <div className="flex flex-wrap gap-2">
                  {myChars.map((c) => (
                    <button key={c.id} onClick={() => claimWithChar(c.id)} disabled={busy}
                      className="px-2.5 py-1 rounded-full text-xs border"
                      style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}>
                      {c.name}
                    </button>
                  ))}
                  {localChars.length > 0 && (
                    <span className="basis-full text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                      本机角色（导入到房主主机）
                    </span>
                  )}
                  {localChars.map((c) => (
                    <button key={`local-${c.id}`} onClick={() => void importAndClaim(c)} disabled={busy}
                      className="btn-secondary !px-2.5 !py-1 text-xs"
                      title="复制到房主主机并入座">
                      {c.name} · 导入
                    </button>
                  ))}
                  <button onClick={generateAndClaim} disabled={busy} className="btn-secondary !px-2 !py-1 text-xs inline-flex items-center gap-1">
                    {busy ? '处理中…' : <><Sparkles size={12} /> AI 生成角色并入座</>}
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
              onKeyDown={(e) => {
                // 输入法合成中（中文选词/确认）的回车不当作发送
                if (e.nativeEvent.isComposing) return
                if (e.key === 'Enter') { e.preventDefault(); sendChat() }
              }}
              placeholder={mySeat?.character_id ? '说点什么…' : '选择角色后可发言'}
              disabled={!mySeat?.character_id}
              className="input flex-1"
            />
            <button onClick={sendChat} disabled={!mySeat?.character_id || !chatInput.trim()} className="btn-primary">发送</button>
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
          {evaluation && (
            <div className="mx-3 mt-3 rounded border p-2 text-xs" style={{ borderColor: evaluation.compatible ? 'var(--color-success)' : 'var(--color-danger)' }}>
              <div className="font-semibold mb-1" style={{ color: evaluation.compatible ? 'var(--color-success)' : 'var(--color-danger)' }}>
                {evaluation.compatible ? '适合本模组' : '存在适配风险'}
              </div>
              {evaluation.warnings.length > 0 && <div className="mb-1">问题：{evaluation.warnings.join('；')}</div>}
              {evaluation.suggestions.length > 0 && <div>建议：{evaluation.suggestions.join('；')}</div>}
            </div>
          )}
          <CharacterPanel character={panelChar} />
        </aside>
      )}
    </div>
  )
}
