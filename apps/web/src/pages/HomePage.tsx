import { Link } from 'react-router-dom'
import { Dices, Sparkles, Upload } from 'lucide-react'

export function HomePage() {
  return (
    <div className="max-w-2xl mx-auto mt-16 text-center">
      <h1
        className="text-4xl font-bold mb-2 tracking-wider"
        style={{ fontFamily: 'var(--font-title)', color: 'var(--color-text-accent)' }}
      >
        TRPG Player
      </h1>
      <p className="mb-10 text-lg" style={{ color: 'var(--color-text-secondary)' }}>
        AI 驱动的跑团平台
      </p>
      <div className="flex flex-wrap gap-3 justify-center">
        <Link to="/onboarding" className="btn-primary flex items-center gap-2 !px-6 !py-3 !text-base">
          <Sparkles className="h-5 w-5" aria-hidden="true" /> 体验新手团
        </Link>
        <Link to="/game" className="btn-secondary flex items-center gap-2 !px-6 !py-3 !text-base">
          <Dices className="h-5 w-5" aria-hidden="true" /> 开始游戏
        </Link>
        <Link to="/modules" className="btn-secondary flex items-center gap-2 !px-6 !py-3 !text-base">
          <Upload className="h-5 w-5" aria-hidden="true" /> 上传模组
        </Link>
      </div>
    </div>
  )
}
