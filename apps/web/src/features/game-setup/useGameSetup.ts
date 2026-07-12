import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getServerUrl, setServerUrl } from '@/api/client'
import { useModuleStore } from '@/stores/moduleStore'
import { useSessionStore } from '@/stores/sessionStore'
import {
  createEmptyModuleFilters,
  filterModules,
  hasModuleFilters,
  moduleFilterOptions,
  parsePlayerRange,
} from './moduleFilters'
import type { ModuleFilters, SetupCharacter, SetupSeat } from './types'

interface RoomInfo {
  id: string
}

export function useGameSetup() {
  const { createSession, fetchSessions, sessions } = useSessionStore()
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const [heroes, setHeroes] = useState<SetupCharacter[]>([])
  const [allies, setAllies] = useState<SetupCharacter[]>([])
  const [moduleId, setModuleId] = useState('')
  const [seats, setSeats] = useState<SetupSeat[]>([])
  const [seatHints, setSeatHints] = useState<Record<number, string>>({})
  const [generatingSeat, setGeneratingSeat] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [joinCode, setJoinCode] = useState('')
  const [hostAddr, setHostAddr] = useState(getServerUrl())
  const [filters, setFilters] = useState<ModuleFilters>(createEmptyModuleFilters)

  const filteredModules = useMemo(
    () => filterModules(modules, filters),
    [filters, modules],
  )
  const filterOptions = useMemo(() => moduleFilterOptions(modules), [modules])
  const selectedModule = modules.find((module) => module.id === moduleId)
  const range = parsePlayerRange(selectedModule?.world_setting)
  const minSeats = Math.max(range.min, 1)
  const usedIds = seats.map((seat) => seat.charId).filter(Boolean)

  const refreshCharacters = useCallback(async () => {
    const [availableHeroes, availableAllies] = await Promise.all([
      api.get<SetupCharacter[]>('/characters?available=true&is_player=true'),
      api.get<SetupCharacter[]>('/characters?available=true&is_player=false'),
    ])
    setHeroes(availableHeroes)
    setAllies(availableAllies)
  }, [])

  useEffect(() => {
    void fetchModules()
    void fetchSessions()
    void refreshCharacters()
  }, [fetchModules, fetchSessions, refreshCharacters])

  const setFilter = (key: keyof ModuleFilters, value: string) => {
    setFilters((current) => ({ ...current, [key]: value }))
  }

  const onSelectModule = (value: string) => {
    setModuleId(value)
    setError('')
    const moduleRange = parsePlayerRange(
      modules.find((module) => module.id === value)?.world_setting,
    )
    const count = Math.max(moduleRange.min, 1)
    setSeats(Array.from({ length: count }, (_, index) => ({
      role: index === 0 ? 'human' : 'ai',
      charId: '',
    })))
  }

  const changeSeatCount = (delta: number) => {
    setSeats((current) => {
      const target = Math.max(minSeats, Math.min(range.max, current.length + delta))
      const next = current.slice(0, target)
      while (next.length < target) next.push({ role: 'ai', charId: '' })
      if (next[0]) next[0] = { ...next[0], role: 'human' }
      return next
    })
  }

  const assignSeat = (index: number, characterId: string) => {
    setSeats((current) => current.map((seat, seatIndex) => (
      seatIndex === index ? { ...seat, charId: characterId } : seat
    )))
  }

  const seatOptions = (index: number): SetupCharacter[] => {
    const pool = seats[index].role === 'human' ? heroes : allies
    return pool.filter((character) => (
      character.id === seats[index].charId || !usedIds.includes(character.id)
    ))
  }

  const generateForSeat = async (index: number) => {
    if (!moduleId || generatingSeat !== null) return
    const isPlayer = seats[index].role === 'human'
    setGeneratingSeat(index)
    setError('')
    try {
      const draft = await api.post<Record<string, unknown>>('/characters/ai-generate', {
        module_id: moduleId,
        hint: (seatHints[index] || '').trim(),
        is_player: isPlayer,
      })
      const created = await api.post<SetupCharacter>('/characters', {
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
      if (isPlayer) setHeroes((current) => [created, ...current])
      else setAllies((current) => [created, ...current])
      assignSeat(index, created.id)
      setSeatHints((current) => ({ ...current, [index]: '' }))
      toast.success(
        `AI 生成「${created.name}」并填入${index === 0 ? '房主' : `队友${index}`}席位`,
      )
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'AI 生成角色失败')
    } finally {
      setGeneratingSeat(null)
    }
  }

  const joinRoom = async () => {
    const code = joinCode.trim().toUpperCase()
    if (!code) return
    setError('')
    let host = hostAddr.trim()
    if (host && !/^https?:\/\//.test(host)) host = `http://${host}`
    if (host && !/:\d+$/.test(host)) host = `${host}:8000`
    setServerUrl(host)
    try {
      const room = await api.get<RoomInfo>(`/sessions/by-code/${code}`)
      navigate(`/room/${room.id}`)
    } catch (reason: unknown) {
      setError(
        reason instanceof Error
          ? reason.message
          : '加入房间失败（检查主机地址与房间码、确认同一局域网）',
      )
    }
  }

  const disconnectHost = () => {
    setServerUrl('')
    setHostAddr('')
    setError('')
    void fetchModules()
    void fetchSessions()
    void refreshCharacters()
  }

  const allSeatsFilled = seats.length > 0 && seats.every((seat, index) => {
    if (index === 0) return Boolean(seat.charId)
    if (seat.role === 'human') return true
    return Boolean(seat.charId)
  })

  const setSeatRole = (index: number, role: 'human' | 'ai') => {
    setSeats((current) => current.map((seat, seatIndex) => (
      seatIndex === index
        ? { role, charId: role === 'human' ? '' : seat.charId }
        : seat
    )))
  }

  const startGame = async () => {
    if (!moduleId || !allSeatsFilled) return
    setError('')
    try {
      const participants = seats.map((seat, index) => ({
        character_id: index > 0 && seat.role === 'human' && !seat.charId
          ? null
          : seat.charId,
        role: seat.role,
        is_primary: index === 0,
      }))
      const session = await createSession(moduleId, participants)
      if (session.status === 'setup') navigate(`/room/${session.id}`)
      else navigate(`/game/${session.id}`, { state: { isNew: true } })
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '创建游戏失败')
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

  const activeSessions = sessions.filter((session) => (
    session.status === 'active' || session.status === 'paused' || session.status === 'setup'
  ))

  return {
    modules,
    filteredModules,
    filters,
    filterOptions,
    hasFilter: hasModuleFilters(filters),
    setFilter,
    resetFilters: () => setFilters(createEmptyModuleFilters()),
    moduleId,
    selectedModule,
    range,
    minSeats,
    seats,
    seatHints,
    setSeatHint: (index: number, value: string) => {
      setSeatHints((current) => ({ ...current, [index]: value }))
    },
    generatingSeat,
    error,
    onSelectModule,
    changeSeatCount,
    assignSeat,
    seatOptions,
    generateForSeat,
    setSeatRole,
    allSeatsFilled,
    startGame,
    joinCode,
    setJoinCode,
    hostAddr,
    setHostAddr,
    connectedHost: getServerUrl(),
    joinRoom,
    disconnectHost,
    activeSessions,
    openSession: (session: { id: string; status: string }) => navigate(
      session.status === 'setup' ? `/room/${session.id}` : `/game/${session.id}`,
    ),
    deleteSession,
  }
}

export type GameSetupState = ReturnType<typeof useGameSetup>
