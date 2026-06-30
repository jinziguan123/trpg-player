import { useState } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { GiCancel } from 'react-icons/gi'
import { useWeapons, type WeaponDef } from './useCocData'

/** 从 CoC 武器表中挑选一件武器：先选大类，再在类下搜索/挑选。 */
export function WeaponPickerDialog({
  open, onOpenChange, onPick,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onPick: (w: WeaponDef) => void
}) {
  const data = useWeapons()
  const weapons = data?.weapons ?? null
  const cats = data?.categories ?? []
  const [cat, setCat] = useState<string>('')   // 选中的大类（空=全部）
  const [q, setQ] = useState('')

  const kw = q.trim()
  const list = (weapons || []).filter((w) => {
    if (cat && w.category !== cat) return false
    return !kw || w.name.includes(kw) || w.skill.includes(kw)
  })

  const chip = (active: boolean) => ({
    borderColor: active ? 'var(--color-success)' : 'var(--color-border)',
    background: active ? 'var(--color-success)' : 'var(--color-bg-tertiary)',
    color: active ? '#fff' : 'var(--color-text-primary)',
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="!max-w-lg flex flex-col" style={{ maxHeight: '80vh' }}>
        <DialogHeader>
          <DialogTitle>从武器表选择</DialogTitle>
        </DialogHeader>

        {/* 大类筛选 */}
        <div className="flex flex-wrap gap-1.5 mb-2">
          <button onClick={() => setCat('')} className="text-xs px-2 py-1 rounded border" style={chip(cat === '')}>全部</button>
          {cats.map((c) => (
            <button key={c} onClick={() => setCat(c)} className="text-xs px-2 py-1 rounded border" style={chip(cat === c)}>
              {c}
            </button>
          ))}
        </div>

        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索武器名 / 使用技能…"
          className="w-full px-2 py-1 rounded text-sm mb-2"
          style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
        />
        <div className="flex-1 overflow-y-auto space-y-1" style={{ maxHeight: '52vh' }}>
          {weapons === null && <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载中…</p>}
          {list.map((w, i) => (
            <button
              key={w.name + i}
              onClick={() => { onPick(w); onOpenChange(false) }}
              className="w-full text-left px-2 py-1.5 rounded text-sm hover:bg-[var(--color-accent)] hover:text-white transition-colors"
              style={{ background: 'var(--color-bg-tertiary)' }}
            >
              <div className="flex justify-between gap-2">
                <span className="font-semibold">{w.name}</span>
                <span className="font-mono text-xs" style={{ opacity: 0.8 }}>{w.dam}</span>
              </div>
              <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {w.skill}{w.range ? ` · 射程 ${w.range}` : ''}{w.tho ? ' · 贯穿' : ''}{w.time ? ` · ${w.time}` : ''}
              </div>
            </button>
          ))}
          {weapons && list.length === 0 && (
            <p className="text-sm text-center py-4" style={{ color: 'var(--color-text-secondary)' }}>无匹配武器</p>
          )}
        </div>
        <DialogFooter>
          <button onClick={() => onOpenChange(false)} className="btn-secondary flex items-center gap-1 text-sm">
            <GiCancel /> 关闭
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
