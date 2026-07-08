import { Component, type ErrorInfo, type ReactNode } from 'react'
import { GiBrokenBone, GiReturnArrow } from 'react-icons/gi'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/** 根级错误边界：任何渲染异常在此兜住，白屏变为可恢复的错误页（重载 / 回首页），
 *  避免桌面分发场景下用户面对一片空白无从自救。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary] 渲染异常：', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div
        className="flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center"
        style={{ background: 'var(--color-bg-primary)', color: 'var(--color-text-primary)' }}
      >
        <GiBrokenBone size={48} style={{ color: 'var(--color-danger)' }} />
        <h1 className="text-xl" style={{ fontFamily: 'var(--font-title)' }}>
          出了点岔子
        </h1>
        <p className="max-w-md text-sm" style={{ color: 'var(--color-text-secondary)' }}>
          页面遇到一个意外错误。你的跑团进度已保存在后端，重载页面通常即可恢复。
        </p>
        <pre
          className="max-w-md overflow-auto rounded px-3 py-2 text-left text-xs"
          style={{ background: 'var(--color-input-bg)', color: 'var(--color-text-secondary)' }}
        >
          {this.state.error.message || String(this.state.error)}
        </pre>
        <div className="flex gap-3">
          <button
            className="btn-primary flex items-center gap-1"
            onClick={() => window.location.reload()}
          >
            重载页面
          </button>
          <button
            className="btn-secondary flex items-center gap-1"
            onClick={() => {
              window.location.href = '/'
            }}
          >
            <GiReturnArrow /> 回首页
          </button>
        </div>
      </div>
    )
  }
}
