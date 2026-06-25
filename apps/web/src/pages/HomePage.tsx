import { Link } from 'react-router-dom'
import { GiScrollUnfurled, GiDiceTwentyFacesTwenty } from 'react-icons/gi'

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
      <div className="flex gap-4 justify-center">
        <Link to="/modules" className="btn-primary flex items-center gap-2 !px-6 !py-3 !text-base">
          <GiScrollUnfurled /> 上传模组
        </Link>
        <Link to="/game" className="btn-secondary flex items-center gap-2 !px-6 !py-3 !text-base">
          <GiDiceTwentyFacesTwenty /> 开始游戏
        </Link>
      </div>
    </div>
  )
}
