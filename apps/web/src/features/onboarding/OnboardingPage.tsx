import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle, RefreshCw, Settings } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { checkAIStatus, startOnboarding } from './api'

type OnboardingState = 'checking' | 'needs_config' | 'creating' | 'error'

export function OnboardingPage() {
  const navigate = useNavigate()
  const [state, setState] = useState<OnboardingState>('checking')

  const run = useCallback(async () => {
    setState('checking')
    try {
      const status = await checkAIStatus()
      if (!status.configured) {
        setState('needs_config')
        return
      }

      setState('creating')
      const session = await startOnboarding()
      navigate(`/game/${session.session_id}`, { state: { isNew: true }, replace: true })
    } catch {
      setState('error')
    }
  }, [navigate])

  useEffect(() => {
    void run()
  }, [run])

  return (
    <main className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center px-4 text-center">
      {(state === 'checking' || state === 'creating') && (
        <>
          <LoaderCircle className="mb-5 h-9 w-9 animate-spin" aria-hidden="true" />
          <h1 className="page-title">
            {state === 'checking' ? '正在检查 AI 配置' : '正在准备新手团'}
          </h1>
          <p style={{ color: 'var(--color-text-secondary)' }}>
            {state === 'checking' ? '确认模型可用后会自动继续。' : '正在准备原创模组与预设调查员。'}
          </p>
        </>
      )}

      {state === 'needs_config' && (
        <>
          <Settings className="mb-5 h-9 w-9" aria-hidden="true" />
          <h1 className="page-title">需要先配置 AI</h1>
          <p className="mb-6" style={{ color: 'var(--color-text-secondary)' }}>
            新手团需要可用的 AI 模型来担任主持人。
          </p>
          <button
            className="btn-primary flex items-center gap-2"
            onClick={() => navigate('/settings', { state: { returnTo: '/onboarding' } })}
          >
            <Settings className="h-4 w-4" aria-hidden="true" />
            配置 AI
          </button>
        </>
      )}

      {state === 'error' && (
        <>
          <RefreshCw className="mb-5 h-9 w-9" aria-hidden="true" />
          <h1 className="page-title">未能启动新手团</h1>
          <p className="mb-6" style={{ color: 'var(--color-text-secondary)' }}>
            请检查本地服务状态后重试，已创建的示例内容不会重复生成。
          </p>
          <button className="btn-primary flex items-center gap-2" onClick={() => void run()}>
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            重试
          </button>
        </>
      )}
    </main>
  )
}
