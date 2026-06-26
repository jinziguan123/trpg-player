import { Toaster as Sonner } from 'sonner'

export function Toaster() {
  return (
    <Sonner
      position="top-right"
      toastOptions={{
        style: {
          background: 'var(--color-bg-card)',
          border: '1px solid var(--color-border)',
          color: 'var(--color-text-primary)',
          fontFamily: 'var(--font-body)',
          fontSize: '0.875rem',
          boxShadow: '2px 4px 12px rgba(90, 60, 20, 0.15)',
        },
      }}
    />
  )
}
