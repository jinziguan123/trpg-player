import { useState } from 'react'
import { ArrowLeft, Plus } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { JoinRoomPanel } from '@/features/game-setup/JoinRoomPanel'
import { NewGamePanel } from '@/features/game-setup/NewGamePanel'
import { SessionList } from '@/features/game-setup/SessionList'
import { useGameSetup } from '@/features/game-setup/useGameSetup'

export function GamePage() {
  const navigate = useNavigate()
  const setup = useGameSetup()
  const [showNew, setShowNew] = useState(false)

  return (
    <div className="mx-auto mt-8 max-w-2xl">
      <div className="mb-6 flex items-center gap-3">
        <button
          onClick={() => navigate(-1)}
          className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm"
        >
          <ArrowLeft size={14} aria-hidden="true" /> 返回
        </button>
        <h2 className="page-title !mb-0">开始游戏</h2>
        <button
          onClick={() => setShowNew((current) => !current)}
          className="btn-primary ml-auto flex items-center gap-1 text-sm"
        >
          <Plus size={14} aria-hidden="true" /> {showNew ? '收起' : '新增游戏'}
        </button>
      </div>

      {showNew && (
        <>
          <NewGamePanel setup={setup} />
          <JoinRoomPanel setup={setup} />
        </>
      )}

      <SessionList setup={setup} />
    </div>
  )
}
