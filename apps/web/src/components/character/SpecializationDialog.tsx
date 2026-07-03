import { useEffect, useState } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { GiCancel, GiCheckMark } from 'react-icons/gi'
import { useSpecializations } from './useCocData'

const TITLE: Record<string, string> = {
  母语: '选择语言', 外语: '选择语言',
  格斗: '选择格斗类型', 射击: '选择射击类型',
}
const SUBTITLE: Record<string, string> = {
  母语: '可用语言：', 外语: '可用语言：',
  格斗: '可用格斗类型：', 射击: '可用射击类型：',
}

/**
 * 专精选择弹窗：给定基名（如「格斗」），列出可选专精项，单选后回调。
 * 复刻克苏鲁公社生成器的「选择类型」交互。
 */
export function SpecializationDialog({
  base, open, onOpenChange, onConfirm, disabledItems,
}: {
  base: string
  open: boolean
  onOpenChange: (v: boolean) => void
  /** 回调：(专精名, 该专精起始值) */
  onConfirm: (spec: string, init: number) => void
  /** 已存在、置灰不可重复选的专精名 */
  disabledItems?: string[]
}) {
  const spec = useSpecializations()
  const cat = spec?.categories[base]
  const [picked, setPicked] = useState<string>('')

  useEffect(() => { if (open) setPicked('') }, [open, base])

  const disabled = new Set(disabledItems || [])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="!max-w-md">
        <DialogHeader>
          <DialogTitle>{TITLE[base] || '选择类型'}</DialogTitle>
        </DialogHeader>
        <p className="text-sm mb-2" style={{ color: 'var(--color-text-secondary)' }}>
          {SUBTITLE[base] || '可用类型：'}
        </p>
        <div className="grid grid-cols-3 gap-2 max-h-[50vh] overflow-y-auto">
          {(cat?.items || []).map((it) => {
            const isDisabled = disabled.has(it.name)
            const isPicked = picked === it.name
            return (
              <button
                key={it.name}
                disabled={isDisabled}
                onClick={() => setPicked(it.name)}
                className="px-2 py-3 rounded border text-sm transition-colors"
                style={{
                  borderColor: isPicked ? 'var(--color-success)' : 'var(--color-border)',
                  background: isPicked ? 'var(--color-success)' : 'var(--color-bg-tertiary)',
                  color: isPicked ? 'var(--color-on-accent)' : isDisabled ? 'var(--color-text-secondary)' : 'var(--color-text-primary)',
                  opacity: isDisabled ? 0.4 : 1,
                  cursor: isDisabled ? 'not-allowed' : 'pointer',
                }}
              >
                {it.name}
              </button>
            )
          })}
          {!cat && <p className="col-span-3 text-sm" style={{ color: 'var(--color-text-secondary)' }}>加载中…</p>}
        </div>
        <DialogFooter>
          <button onClick={() => onOpenChange(false)} className="btn-secondary flex items-center gap-1 text-sm">
            <GiCancel /> 取消
          </button>
          <button
            onClick={() => {
              if (!picked || !cat) return
              const item = cat.items.find((i) => i.name === picked)
              onConfirm(picked, item?.init ?? cat.base_init)
              onOpenChange(false)
            }}
            disabled={!picked}
            className="btn-primary flex items-center gap-1 text-sm"
            style={!picked ? { opacity: 0.5 } : undefined}
          >
            <GiCheckMark /> 确认
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
