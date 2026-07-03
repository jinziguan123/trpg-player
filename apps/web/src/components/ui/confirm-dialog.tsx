import { useState } from 'react'
import {
  Dialog, DialogContent, DialogHeader, DialogFooter,
  DialogTitle, DialogDescription,
} from './dialog'

interface ConfirmDialogProps {
  title: string
  description: string
  confirmLabel?: string
  onConfirm: () => void | Promise<void>
  children: (open: () => void) => React.ReactNode
  /** 描述和按钮之间的额外内容（如一个可选输入框），默认无 */
  extra?: React.ReactNode
}

export function ConfirmDialog({
  title, description, confirmLabel = '确认', onConfirm, children, extra,
}: ConfirmDialogProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const handleConfirm = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setLoading(true)
    try {
      await onConfirm()
      setOpen(false)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {children(() => setOpen(true))}
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {extra}
        <DialogFooter>
          <button onClick={() => setOpen(false)} className="btn-secondary" disabled={loading}>
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading}
            className="text-sm px-4 py-1.5 rounded-[3px] font-semibold transition-colors cursor-pointer"
            style={{
              background: 'var(--color-danger-deep)',
              color: 'var(--color-on-danger)',
              border: '1px solid var(--color-danger-deep)',
              opacity: loading ? 0.5 : 1,
            }}
          >
            {loading ? '处理中...' : confirmLabel}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
