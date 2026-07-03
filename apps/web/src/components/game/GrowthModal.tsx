import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { GiUpgrade } from 'react-icons/gi'
import { X } from 'lucide-react'
import { api } from '../../api/client'

interface EligibleSkill { skill: string; value: number }
interface GrowthResult {
  skill: string
  roll: number
  improved: boolean
  gain: number
  old_value: number
  new_value: number
}

/** 成长结算弹窗：列出本局成功用过的技能，一键做成长检定并把成长应用到角色。 */
export function GrowthModal({
  sessionId, characterId, onClose,
}: {
  sessionId: string
  characterId: string
  onClose: () => void
}) {
  const [eligible, setEligible] = useState<EligibleSkill[]>([])
  const [results, setResults] = useState<GrowthResult[] | null>(null)
  const [settling, setSettling] = useState(false)

  const load = useCallback(() => {
    api.get<{ skills: EligibleSkill[] }>(`/sessions/${sessionId}/growth?character_id=${characterId}`)
      .then((r) => setEligible(r.skills || []))
      .catch(() => {})
  }, [sessionId, characterId])

  useEffect(() => { load() }, [load])

  const settle = async () => {
    setSettling(true)
    try {
      const r = await api.post<{ results: GrowthResult[] }>(
        `/sessions/${sessionId}/growth/settle`, { character_id: characterId },
      )
      setResults(r.results || [])
      const up = (r.results || []).filter((x) => x.improved).length
      toast.success(up > 0 ? `成长结算完成：${up} 项技能提升` : '成长结算完成：本次无提升')
    } catch {
      toast.error('成长结算失败，请稍后重试')
    } finally {
      setSettling(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center"
      style={{ paddingTop: '10vh', background: 'rgba(0,0,0,0.6)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg max-h-[78vh] overflow-y-auto rounded-lg border p-5 mx-4"
        style={{ background: 'var(--color-bg-card)', borderColor: 'var(--color-border)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold flex items-center gap-2" style={{ color: 'var(--color-text-accent)' }}>
            <GiUpgrade /> 成长结算
          </h2>
          <button onClick={onClose} className="btn-secondary !px-2 !py-1"><X size={16} /></button>
        </div>

        <p className="text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
          本局成功使用过的技能可做成长检定：d100 大于当前技能值（或大于 95）则 +1d10（上限 99）。
        </p>

        {results === null ? (
          eligible.length === 0 ? (
            <p className="text-sm py-6 text-center" style={{ color: 'var(--color-text-secondary)' }}>
              本局还没有成功使用过的技能，暂无可成长项。
            </p>
          ) : (
            <>
              <ul className="space-y-1 text-sm mb-4" style={{ color: 'var(--color-text-primary)' }}>
                {eligible.map((e) => (
                  <li key={e.skill} className="flex justify-between">
                    <span>{e.skill}</span>
                    <span style={{ color: 'var(--color-text-secondary)' }}>{e.value}</span>
                  </li>
                ))}
              </ul>
              <button onClick={settle} disabled={settling} className="btn-primary w-full !py-1.5 disabled:opacity-50">
                {settling ? '掷骰中…' : `为 ${eligible.length} 项技能掷骰成长`}
              </button>
            </>
          )
        ) : (
          <ul className="space-y-1.5 text-sm" style={{ color: 'var(--color-text-primary)' }}>
            {results.map((r) => (
              <li key={r.skill} className="flex justify-between items-center">
                <span>{r.skill}</span>
                <span className="flex items-center gap-2">
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>d100={r.roll}</span>
                  {r.improved ? (
                    <span className="font-semibold" style={{ color: 'var(--color-dice-gold)' }}>
                      {r.old_value} → {r.new_value}（+{r.gain}）
                    </span>
                  ) : (
                    <span style={{ color: 'var(--color-text-secondary)' }}>未提升（{r.old_value}）</span>
                  )}
                </span>
              </li>
            ))}
            {results.length === 0 && (
              <li className="text-center py-4" style={{ color: 'var(--color-text-secondary)' }}>无可成长技能。</li>
            )}
          </ul>
        )}
      </div>
    </div>
  )
}
