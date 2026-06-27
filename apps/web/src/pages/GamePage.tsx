import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { useSessionStore } from '../stores/sessionStore'
import type { SessionParticipant } from '../stores/sessionStore'
import { useModuleStore } from '../stores/moduleStore'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
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

interface Seat {
  role: 'human' | 'ai'
  charId: string
}

interface RoomInfo {
  id: string
  module_id: string
  module_title?: string
  room_code?: string | null
  participants?: SessionParticipant[]
}

/** 从模组 world_setting.player_count（如 "1-4"、"2-6人"）解析推荐人数范围。 */
function parsePlayerRange(ws?: Record<string, unknown>): { min: number; max: number } {
  const raw = String((ws?.player_count as string | undefined) ?? '')
  const nums = (raw.match(/\d+/g) || []).map(Number).filter((n) => n > 0)
  if (nums.length >= 2) return { min: Math.min(...nums), max: Math.max(...nums) }
  if (nums.length === 1) return { min: 1, max: nums[0] }
  return { min: 1, max: 6 }
}

export function GamePage() {
  const { createSession, fetchSessions, sessions } = useSessionStore()
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const [heroes, setHeroes] = useState<Character[]>([])
  const [allies, setAllies] = useState<Character[]>([])
  const [moduleId, setModuleId] = useState('')
  const [seats, setSeats] = useState<Seat[]>([])
  const [generatingSeat, setGeneratingSeat] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [joinCode, setJoinCode] = useState('')
  const [joinedRoom, setJoinedRoom] = useState<RoomInfo | null>(null)
  const [myChars, setMyChars] = useState<Character[]>([])
  const [claiming, setClaiming] = useState(false)

  const selectedModule = modules.find((m) => m.id === moduleId)
  const range = parsePlayerRange(selectedModule?.world_setting)
  const minSeats = Math.max(range.min, 1)
  const usedIds = seats.map((s) => s.charId).filter(Boolean)

  const refreshCharacters = async () => {
    const [h, a] = await Promise.all([
      api.get<Character[]>('/characters?available=true&is_player=true'),
      api.get<Character[]>('/characters?available=true&is_player=false'),
    ])
    setHeroes(h)
    setAllies(a)
  }

  useEffect(() => {
    fetchModules()
    fetchSessions()
    refreshCharacters()
  }, [fetchModules, fetchSessions])

  const onSelectModule = (v: string) => {
    setModuleId(v)
    setError('')
    const r = parsePlayerRange(modules.find((m) => m.id === v)?.world_setting)
    const n = Math.max(r.min, 1)
    setSeats(Array.from({ length: n }, (_, i) => ({ role: i === 0 ? 'human' : 'ai', charId: '' })))
  }

  const changeSeatCount = (delta: number) => {
    setSeats((prev) => {
      const target = Math.max(minSeats, Math.min(range.max, prev.length + delta))
      const next = prev.slice(0, target)
      while (next.length < target) next.push({ role: 'ai', charId: '' })
      if (next[0]) next[0] = { ...next[0], role: 'human' }
      return next
    })
  }

  const assignSeat = (i: number, charId: string) => {
    setSeats((prev) => prev.map((s, idx) => (idx === i ? { ...s, charId } : s)))
  }

  const seatOptions = (i: number): Character[] => {
    const pool = seats[i].role === 'human' ? heroes : allies
    return pool.filter((c) => c.id === seats[i].charId || !usedIds.includes(c.id))
  }

  const generateForSeat = async (i: number) => {
    if (!moduleId || generatingSeat !== null) return
    const isPlayer = seats[i].role === 'human'
    setGeneratingSeat(i)
    setError('')
    try {
      const draft = await api.post<Record<string, unknown>>('/characters/ai-generate', {
        module_id: moduleId,
        hint: '',
        is_player: isPlayer,
      })
      const created = await api.post<Character>('/characters', {
        name: draft.name,
        module_id: moduleId,
        rule_system: (draft.rule_system as string) || 'coc',
        is_player: isPlayer,
        age: draft.age ?? 25,
        base_attributes: draft.base_attributes,
        skills: draft.skills,
        system_data: draft.system_data,
        backstory: draft.backstory ?? '',
      })
      if (isPlayer) setHeroes((h) => [created, ...h])
      else setAllies((a) => [created, ...a])
      assignSeat(i, created.id)
      toast.success(`AI 生成「${created.name}」并填入${i === 0 ? '主角' : `队友${i}`}席位`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'AI 生成角色失败')
    } finally {
      setGeneratingSeat(null)
    }
  }

  // —— 加入房间（2b）——
  const joinRoom = async () => {
    const code = joinCode.trim().toUpperCase()
    if (!code) return
    setError('')
    try {
      const room = await api.get<RoomInfo>(`/sessions/by-code/${code}`)
      if (room.participants?.some((p) => p.is_mine)) {
        navigate(`/game/${room.id}`)
        return
      }
      setJoinedRoom(room)
      const mine = await api.get<Character[]>('/characters?available=true&is_player=true&mine=true')
      setMyChars(mine)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '加入房间失败')
    }
  }

  const claimAndEnter = async (charId: string) => {
    if (!joinedRoom) return
    const seat = joinedRoom.participants?.find((p) => p.role === 'human' && !p.character_id && !p.claimed)
    if (!seat) { setError('房间已满，没有空席'); return }
    setClaiming(true)
    try {
      await api.post(`/sessions/${joinedRoom.id}/claim`, { seat_order: seat.seat_order, character_id: charId })
      navigate(`/game/${joinedRoom.id}`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '入座失败')
    } finally {
      setClaiming(false)
    }
  }

  const generateAndClaim = async () => {
    if (!joinedRoom) return
    setClaiming(true)
    setError('')
    try {
      const draft = await api.post<Record<string, unknown>>('/characters/ai-generate', {
        module_id: joinedRoom.module_id, hint: '', is_player: true,
      })
      const created = await api.post<Character>('/characters', {
        name: draft.name, module_id: joinedRoom.module_id, rule_system: (draft.rule_system as string) || 'coc',
        is_player: true, age: draft.age ?? 25, base_attributes: draft.base_attributes,
        skills: draft.skills, system_data: draft.system_data, backstory: draft.backstory ?? '',
      })
      await claimAndEnter(created.id)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'AI 生成角色失败')
      setClaiming(false)
    }
  }

  // 主角必填；AI 席必填；留空待加入(真人)席可空
  const allSeatsFilled = seats.length > 0 && seats.every((s, i) => {
    if (i === 0) return !!s.charId
    if (s.role === 'human') return true
    return !!s.charId
  })

  const setSeatRole = (i: number, role: 'human' | 'ai') => {
    setSeats((prev) => prev.map((s, idx) => (idx === i ? { role, charId: role === 'human' ? '' : s.charId } : s)))
  }

  const startGame = async () => {
    if (!moduleId || !allSeatsFilled) return
    setError('')
    try {
      const participants = seats.map((s, i) => ({
        character_id: i > 0 && s.role === 'human' && !s.charId ? null : s.charId,
        role: s.role,
        is_primary: i === 0,
      }))
      const session = await createSession(moduleId, participants)
      navigate(`/game/${session.id}`, { state: { isNew: true } })
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '创建游戏失败'
      setError(msg)
    }
  }

  const deleteSession = async (sessionId: string) => {
    try {
      await api.delete(`/sessions/${sessionId}`)
      await fetchSessions()
      await refreshCharacters()
      toast.success('游戏存档已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const formatTime = (ts?: string) => {
    if (!ts) return ''
    const d = new Date(ts)
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }

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

        <Select value={moduleId} onValueChange={onSelectModule}>
          <SelectTrigger className="w-full mb-3">
            <SelectValue placeholder="— 选择模组 —" />
          </SelectTrigger>
          <SelectContent>
            {modules.map((m) => <SelectItem key={m.id} value={m.id}>{m.title}</SelectItem>)}
          </SelectContent>
        </Select>

        {moduleId && (
          <>
            {/* 第一步：按模组推荐人数设置玩家席位数 */}
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-medium">玩家人数</span>
              <button
                onClick={() => changeSeatCount(-1)}
                disabled={seats.length <= minSeats}
                className="btn-secondary !px-2 !py-0.5 disabled:opacity-40"
              >−</button>
              <span className="w-6 text-center font-semibold" style={{ color: 'var(--color-text-accent)' }}>
                {seats.length}
              </span>
              <button
                onClick={() => changeSeatCount(1)}
                disabled={seats.length >= range.max}
                className="btn-secondary !px-2 !py-0.5 disabled:opacity-40"
              >＋</button>
            </div>
            <p className="text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
              本模组推荐 {range.min}–{range.max} 人 · KP 由 AI 担任 ·
              你 1 人 + AI 队友 {Math.max(seats.length - 1, 0)} 人
              {range.min === 1 && range.max === 6 && !selectedModule?.world_setting?.player_count
                ? '（模组未标注人数，按默认范围）' : ''}
            </p>

            {/* 第二步：逐个席位填入角色（非主角席可设为 AI 或「留空待真人加入」） */}
            <div className="mb-3">
              {seats.map((seat, i) => {
                const emptyHuman = i > 0 && seat.role === 'human'
                return (
                  <div key={i} className="flex items-center gap-2 mb-2">
                    <span
                      className="badge whitespace-nowrap"
                      style={i === 0 ? { borderColor: 'var(--color-accent)', color: 'var(--color-text-accent)' } : undefined}
                    >
                      {i === 0 ? '★ 你（真人）' : emptyHuman ? `🪑 真人 ${i}` : `🤖 AI 队友 ${i}`}
                    </span>
                    {emptyHuman ? (
                      <span className="flex-1 text-xs italic" style={{ color: 'var(--color-text-secondary)' }}>
                        留空 · 开局后分享房间码，等真人加入认领
                      </span>
                    ) : (
                      <Select value={seat.charId} onValueChange={(v) => assignSeat(i, v)}>
                        <SelectTrigger className="flex-1">
                          <SelectValue placeholder={i === 0 ? '选择你的角色' : '选择 AI 队友角色'} />
                        </SelectTrigger>
                        <SelectContent>
                          {seatOptions(i).map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    )}
                    {i > 0 && (
                      <button
                        onClick={() => setSeatRole(i, emptyHuman ? 'ai' : 'human')}
                        className="btn-secondary !px-2 !py-1 text-xs whitespace-nowrap"
                        title="在「AI 队友」与「留空待真人加入」之间切换"
                      >
                        {emptyHuman ? '改为 AI' : '设为真人空席'}
                      </button>
                    )}
                    {!emptyHuman && (
                      <button
                        onClick={() => generateForSeat(i)}
                        disabled={generatingSeat !== null}
                        className="btn-secondary !px-2 !py-1 text-xs whitespace-nowrap"
                        title="让 AI 现场生成一张贴合模组的角色卡填入此席位"
                      >
                        {generatingSeat === i ? '生成中…' : '✨ 生成'}
                      </button>
                    )}
                  </div>
                )
              })}
            </div>

            {error && (
              <p className="text-sm mb-2" style={{ color: 'var(--color-danger)' }}>{error}</p>
            )}
            <button onClick={startGame} disabled={!allSeatsFilled} className="btn-primary">
              开始冒险（{seats.length} 名玩家）
            </button>
          </>
        )}
      </div>

      {/* 加入他人房间（联机） */}
      <div className="card mb-6">
        <h3 className="card-title">加入房间</h3>
        {!joinedRoom ? (
          <div className="flex gap-2">
            <input
              value={joinCode}
              onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
              onKeyDown={(e) => { if (e.key === 'Enter') joinRoom() }}
              placeholder="输入房间码（向房主索取）"
              className="input flex-1"
              maxLength={8}
            />
            <button onClick={joinRoom} disabled={!joinCode.trim()} className="btn-primary">加入</button>
          </div>
        ) : (
          <div>
            <p className="text-sm mb-2">
              已找到房间「{joinedRoom.module_title || '未知模组'}」，请选择你的角色入座空席：
            </p>
            <div className="flex flex-wrap gap-2 mb-2">
              {myChars.map((c) => (
                <button
                  key={c.id}
                  onClick={() => claimAndEnter(c.id)}
                  disabled={claiming}
                  className="px-2.5 py-1 rounded-full text-xs border"
                  style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
                >
                  {c.name}
                </button>
              ))}
              <button onClick={generateAndClaim} disabled={claiming} className="btn-secondary !px-2 !py-1 text-xs">
                {claiming ? '入座中…' : '✨ AI 生成角色并入座'}
              </button>
            </div>
            <button onClick={() => { setJoinedRoom(null); setJoinCode('') }} className="btn-secondary !px-2 !py-1 text-xs">
              取消
            </button>
          </div>
        )}
        {error && joinedRoom && (
          <p className="text-sm mt-2" style={{ color: 'var(--color-danger)' }}>{error}</p>
        )}
      </div>

      {activeSessions.length > 0 && (
        <div>
          <h3 className="card-title">继续游戏</h3>
          {activeSessions.map((s) => (
            <div
              key={s.id}
              onClick={() => navigate(`/game/${s.id}`)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter') navigate(`/game/${s.id}`) }}
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
