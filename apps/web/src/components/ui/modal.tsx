import { useEffect, useLayoutEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/**
 * 通用悬浮窗：portal 到 document.body，避开 `.route-fade`（<main> 带 transform/will-change）
 * 造成的「fixed 被限制在内容区」陷阱——遮罩因此能盖住整个视口（含左侧菜单栏）。
 * 窗体以**主内容区（<main>，即游戏聊天界面）**为中心居中（避开侧栏偏移）；不透明。
 */
export function Modal({
  onClose,
  children,
  widthClass = 'max-w-xl',
  align = 'center',
  padded = false,
}: {
  onClose: () => void
  children: ReactNode
  widthClass?: string
  align?: 'center' | 'top'
  padded?: boolean
}) {
  // 居中锚定到 <main> 左边缘：侧栏可折叠、宽度不固定，实测其 rect 比硬编码偏移可靠。
  const [left, setLeft] = useState(0)
  useLayoutEffect(() => {
    const measure = () => {
      const m = document.querySelector('main')
      setLeft(m ? m.getBoundingClientRect().left : 0)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      className="fixed inset-0 z-[100]"
      style={{ background: 'rgba(0,0,0,0.72)' }}
      onClick={onClose}
    >
      <div
        className={`fixed flex justify-center ${align === 'top' ? 'items-start' : 'items-center'}`}
        style={{ top: 0, bottom: 0, left, right: 0, paddingTop: align === 'top' ? '10vh' : undefined }}
      >
        <div
          className={`w-full ${widthClass} mx-4 rounded-lg shadow-2xl overflow-hidden ${padded ? 'p-5' : ''}`}
          style={{
            // 双层背景保证**不透明**（浅色主题 --color-bg-card 带 alpha）：不透明底 + 卡片色
            background: 'var(--color-bg-tertiary)',
            backgroundImage: 'linear-gradient(var(--color-bg-card), var(--color-bg-card))',
            border: '1px solid var(--color-border)',
            maxHeight: '86vh',
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {children}
        </div>
      </div>
    </div>,
    document.body,
  )
}
