import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { GiScrollUnfurled } from 'react-icons/gi'
import { X } from 'lucide-react'
import { api } from '../../api/client'

interface Recap {
  title: string
  key_decisions: string[]
  clues_resolved: string[]
  clues_unresolved: string[]
  highlights: { seq: number | null; quote: string }[]
  casualties: string[]
  up_to_seq?: number
  generated_at?: string
}

function Section({ label, items }: { label: string; items: string[] }) {
  if (!items?.length) return null
  return (
    <div className="mb-3">
      <div className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>{label}</div>
      <ul className="list-disc pl-5 space-y-0.5 text-sm" style={{ color: 'var(--color-text-primary)' }}>
        {items.map((t, i) => <li key={i}>{t}</li>)}
      </ul>
    </div>
  )
}

/** 战报（章节小结）弹窗：列出已生成战报（最新在上），可一键生成新战报。 */
export function RecapModal({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [recaps, setRecaps] = useState<Recap[]>([])
  const [genning, setGenning] = useState(false)
  const [exporting, setExporting] = useState<string | null>(null)

  const exportReplay = async (style: 'novel' | 'script') => {
    setExporting(style)
    try {
      const r = await api.get<{ markdown: string; title: string }>(`/sessions/${sessionId}/replay?style=${style}`)
      const blob = new Blob([r.markdown], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${r.title || '团记'}-${style === 'novel' ? '小说体' : '剧本体'}.md`
      a.click()
      URL.revokeObjectURL(url)
      toast.success('团记已导出')
    } catch {
      toast.error('团记导出失败，请稍后重试')
    } finally {
      setExporting(null)
    }
  }

  const load = useCallback(() => {
    api.get<{ recaps: Recap[] }>(`/sessions/${sessionId}/recaps`)
      .then((r) => setRecaps(r.recaps || []))
      .catch(() => {})
  }, [sessionId])

  useEffect(() => { load() }, [load])

  const generate = async () => {
    setGenning(true)
    try {
      await api.post(`/sessions/${sessionId}/recap`)
      load()
      toast.success('战报已生成')
    } catch {
      toast.error('战报生成失败，请稍后重试')
    } finally {
      setGenning(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center"
      style={{ paddingTop: '8vh', background: 'rgba(0,0,0,0.6)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[80vh] overflow-y-auto rounded-lg border p-5 mx-4"
        style={{ background: 'var(--color-bg-card)', borderColor: 'var(--color-border)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold flex items-center gap-2" style={{ color: 'var(--color-text-accent)' }}>
            <GiScrollUnfurled /> 战报 / 章节小结
          </h2>
          <div className="flex items-center gap-2">
            <button onClick={() => exportReplay('novel')} disabled={!!exporting} className="btn-secondary !px-2 !py-1 text-sm disabled:opacity-50" title="把整局改写成小说体 markdown 下载">
              {exporting === 'novel' ? '导出中…' : '导出小说'}
            </button>
            <button onClick={() => exportReplay('script')} disabled={!!exporting} className="btn-secondary !px-2 !py-1 text-sm disabled:opacity-50" title="把整局改写成剧本体 markdown 下载">
              {exporting === 'script' ? '导出中…' : '导出剧本'}
            </button>
            <button onClick={generate} disabled={genning} className="btn-primary !px-3 !py-1 text-sm disabled:opacity-50">
              {genning ? '生成中…' : '生成战报'}
            </button>
            <button onClick={onClose} className="btn-secondary !px-2 !py-1"><X size={16} /></button>
          </div>
        </div>

        {recaps.length === 0 ? (
          <p className="text-sm py-6 text-center" style={{ color: 'var(--color-text-secondary)' }}>
            还没有战报。点「生成战报」把本局经历浓缩成一份结构化小结。
          </p>
        ) : (
          <div className="space-y-5">
            {[...recaps].reverse().map((r, idx) => (
              <div key={idx} className="border-t pt-3 first:border-t-0 first:pt-0" style={{ borderColor: 'var(--color-border)' }}>
                <h3 className="text-sm font-bold mb-2" style={{ color: 'var(--color-text-primary)' }}>{r.title}</h3>
                <Section label="关键抉择" items={r.key_decisions} />
                <Section label="已解线索" items={r.clues_resolved} />
                <Section label="未解悬念" items={r.clues_unresolved} />
                {!!r.highlights?.length && (
                  <div className="mb-3">
                    <div className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-accent)' }}>名场面</div>
                    <ul className="space-y-1 text-sm" style={{ color: 'var(--color-text-primary)' }}>
                      {r.highlights.map((h, i) => (
                        <li key={i} className="italic" style={{ color: 'var(--color-text-secondary)' }}>
                          “{h.quote}”{h.seq != null && <span className="not-italic text-xs"> · #{h.seq}</span>}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <Section label="阵亡与损失" items={r.casualties} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
