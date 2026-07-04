import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { GiCharacter } from 'react-icons/gi'
import { X } from 'lucide-react'
import { api } from '../../api/client'
import { Modal } from '../ui/modal'

interface ImprovisedNpc {
  name: string
  mentions: number
  promoted: boolean
}

/**
 * 临场角色收编（房主专用）：列出 KP 临场添加的龙套，可把出彩的一个「转为正式配角」。
 * 转正会据其既有言行生成 NPC 卡（会话级，不改模组），此后他有记忆、一致人设、可被单独扮演。
 */
export function ImprovisedNpcModal({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [list, setList] = useState<ImprovisedNpc[]>([])
  const [promoting, setPromoting] = useState<string | null>(null)

  const load = useCallback(() => {
    api.get<{ improvised_npcs: ImprovisedNpc[] }>(`/sessions/${sessionId}/improvised-npcs`)
      .then((r) => setList(r.improvised_npcs || []))
      .catch(() => {})
  }, [sessionId])

  useEffect(() => { load() }, [load])

  const promote = async (name: string) => {
    setPromoting(name)
    try {
      await api.post(`/sessions/${sessionId}/improvised-npcs/promote`, { name })
      toast.success(`「${name}」已转为正式配角`)
      load()
    } catch {
      toast.error('转正失败，请稍后重试')
    } finally {
      setPromoting(null)
    }
  }

  return (
    <Modal onClose={onClose} widthClass="max-w-lg" padded>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold flex items-center gap-2" style={{ color: 'var(--color-text-accent)' }}>
          <GiCharacter /> 临场角色
        </h2>
        <button onClick={onClose} className="btn-secondary !px-2 !py-1"><X size={16} /></button>
      </div>
      <p className="text-xs mb-3" style={{ color: 'var(--color-text-secondary)' }}>
        这些是 KP 在跑团中临时添加的龙套。默认他们只是配角（不掌握线索与秘密）。
        若某个龙套确实出彩、你想让他成为固定配角，可「转正」——系统会据他的既有言行生成一致人设，
        此后他有记忆、可被单独扮演（仅记入本局，不改动模组）。
      </p>
      {list.length === 0 ? (
        <p className="text-sm py-6 text-center" style={{ color: 'var(--color-text-secondary)' }}>
          本局暂无临场角色。
        </p>
      ) : (
        <div className="space-y-2">
          {list.map((n) => (
            <div
              key={n.name}
              className="flex items-center gap-2 rounded border px-3 py-2"
              style={{ borderColor: 'var(--color-border)' }}
            >
              <span className="text-sm flex-1" style={{ color: 'var(--color-text-primary)' }}>{n.name}</span>
              <span className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>出场 {n.mentions} 次</span>
              {n.promoted ? (
                <span className="text-[11px] px-1.5 py-0.5 rounded" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-accent)' }}>
                  已转正
                </span>
              ) : (
                <button
                  onClick={() => promote(n.name)}
                  disabled={!!promoting}
                  className="btn-primary !px-2 !py-1 text-xs disabled:opacity-50"
                >
                  {promoting === n.name ? '转正中…' : '转为正式配角'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}
