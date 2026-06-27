import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { useSessionStore } from '../stores/sessionStore'
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

export function GamePage() {
  const { createSession, fetchSessions, sessions } = useSessionStore()
  const { modules, fetchModules } = useModuleStore()
  const navigate = useNavigate()
  const [characters, setCharacters] = useState<Character[]>([])
  const [moduleId, setModuleId] = useState('')
  const [charId, setCharId] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    fetchModules()
    fetchSessions()
    api.get<Character[]>('/characters?available=true').then(setCharacters)
  }, [fetchModules, fetchSessions])

  const startGame = async () => {
    if (!moduleId || !charId) return
    setError('')
    try {
      const session = await createSession(moduleId, charId)
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
      await api.get<Character[]>('/characters?available=true').then(setCharacters)
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
