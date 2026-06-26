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
}

export function ConfirmDialog({
  title, description, confirmLabel = '确认', onConfirm, children,
}: ConfirmDialogProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const handleConfirm = async () => {
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
        <DialogFooter>
          <button onClick={() => setOpen(false)} className="btn-secondary" disabled={loading}>
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading}
            className="text-sm px-4 py-1.5 rounded-[3px] font-semibold transition-colors cursor-pointer"
            style={{
              background: 'var(--color-danger)',
              color: '#f0e6d3',
              border: '1px solid var(--color-danger)',
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
