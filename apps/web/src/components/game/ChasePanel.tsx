import { useState } from 'react'
import { toast } from 'sonner'
import { api } from '../../api/client'
import { GiRun } from 'react-icons/gi'

// 追逐态：形状与后端 chase_state metadata 对齐。
export interface ChaseState {
  round: number
  gap: number
  escape_at: number
  caught_at: number
  quarry: string    // 逃方
  pursuer: string   // 追方
}

export function ChasePanel({ chase, sessionId }: { chase: ChaseState; sessionId: string }) {
  const [submitting, setSubmitting] = useState(false)

  // 距离轨范围：caught_at（被追上）..escape_at（脱身）。当前 gap 归一到 [0,1]。
  const span = chase.escape_at - chase.caught_at
  const ratio = span > 0 ? (chase.gap - chase.caught_at) / span : 0
  const pct = Math.max(0, Math.min(100, ratio * 100))

  const runOnce = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/chase/action`, { type: 'run' })
      // 成功不手动刷新——后端广播新的 chase_state。
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '行动提交失败')
    } finally {
      // 短暂禁用防连点。
      setTimeout(() => setSubmitting(false), 600)
    }
  }

  return (
    <div className="card mx-3 mb-2 !px-3 !py-2.5">
      {/* 顶部：轮次 */}
      <div className="flex items-center gap-2 mb-2.5">
        <GiRun style={{ color: 'var(--color-text-accent)', fontSize: '1.05rem', flexShrink: 0 }} />
        <span className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>追逐 · 第 {chase.round} 轮</span>
      </div>

      {/* 距离轨：追方在追赶侧（危险），逃方在领先侧（脱身）。 */}
      <div className="flex items-center justify-between text-[10px] mb-1">
        <span className="truncate" style={{ color: 'var(--color-danger)' }}>{chase.pursuer}</span>
        <span className="truncate" style={{ color: 'var(--color-accent)' }}>{chase.quarry}</span>
      </div>
      <div className="relative h-2 rounded-full" style={{ background: 'var(--color-input-bg)' }}>
        {/* 已拉开距离的填充（从追赶侧起） */}
        <div
          className="stat-bar-fill absolute inset-y-0 left-0 rounded-full"
          style={{ width: `${pct}%`, background: 'var(--color-accent)' }}
        />
        {/* 当前位置标记点 */}
        <div
          className="absolute top-1/2 rounded-full"
          style={{
            left: `${pct}%`,
            width: 10,
            height: 10,
            transform: 'translate(-50%, -50%)',
            background: 'var(--color-text-accent)',
            border: '1px solid var(--color-bg-secondary)',
            boxShadow: '0 0 6px rgba(212, 162, 78, 0.55)',
          }}
        />
      </div>
      <div className="flex items-center justify-between text-[10px] mt-1">
        <span style={{ color: 'var(--color-danger)' }}>被追上 ≤{chase.caught_at}</span>
        <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>距离 {chase.gap}</span>
        <span style={{ color: 'var(--color-accent)' }}>脱身 ≥{chase.escape_at}</span>
      </div>

      {/* 行动：奔逃一轮 */}
      <div className="mt-2 pt-2 flex justify-end" style={{ borderTop: '1px solid var(--color-border)' }}>
        <button
          onClick={() => void runOnce()}
          disabled={submitting}
          className="btn-primary text-xs !px-2.5 !py-1 flex items-center gap-1"
          style={submitting ? { opacity: 0.5 } : undefined}
        >
          <GiRun size={13} /> 奔逃一轮
        </button>
      </div>
    </div>
  )
}
