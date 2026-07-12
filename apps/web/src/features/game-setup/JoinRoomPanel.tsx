import type { GameSetupState } from './useGameSetup'

export function JoinRoomPanel({ setup }: { setup: GameSetupState }) {
  const {
    connectedHost,
    disconnectHost,
    hostAddr,
    setHostAddr,
    joinCode,
    setJoinCode,
    joinRoom,
  } = setup

  return (
    <div className="card mb-6">
      <h3 className="card-title">加入房间</h3>
      {connectedHost && (
        <div
          className="mb-2 flex items-center gap-2 rounded px-2 py-1 text-xs"
          style={{
            background: 'var(--color-bg-tertiary)',
            color: 'var(--color-text-secondary)',
          }}
        >
          <span>
            已连接到主机 <b style={{ color: 'var(--color-text-accent)' }}>{connectedHost}</b>
          </span>
          <button
            onClick={disconnectHost}
            className="btn-secondary ml-auto !px-2 !py-0.5"
          >
            断开（回本机）
          </button>
        </div>
      )}
      <input
        value={hostAddr}
        onChange={(event) => setHostAddr(event.target.value)}
        placeholder="主机地址（如 192.168.1.5；留空 = 本机房间）"
        className="input mb-2 w-full"
      />
      <div className="flex gap-2">
        <input
          value={joinCode}
          onChange={(event) => setJoinCode(event.target.value.toUpperCase())}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void joinRoom()
          }}
          placeholder="输入房间码（向房主索取）"
          className="input flex-1"
          maxLength={8}
        />
        <button
          onClick={() => void joinRoom()}
          disabled={!joinCode.trim()}
          className="btn-primary"
        >
          加入
        </button>
      </div>
    </div>
  )
}
