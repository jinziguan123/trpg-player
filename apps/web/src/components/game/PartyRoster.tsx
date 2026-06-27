import type { SessionParticipant } from '../../stores/sessionStore'

interface Props {
  participants: SessionParticipant[]
  selectedId?: string | null
  onSelect?: (characterId: string) => void
}

/** 游戏页顶部的队伍条：展示主角与在场 AI 队友，点击可在右侧查看其角色卡。 */
export function PartyRoster({ participants, selectedId, onSelect }: Props) {
  if (!participants || participants.length === 0) return null
  const sorted = [...participants].sort((a, b) => a.seat_order - b.seat_order)

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {sorted.map((p) => {
        const empty = p.role === 'human' && !p.character_id
        const icon = empty ? '🪑' : p.role === 'ai' ? '🤖' : p.is_mine ? '🙋' : '👤'
        const active = !!p.character_id && selectedId === p.character_id
        const highlight = active || p.is_mine
        return (
          <button
            key={p.seat_order}
            onClick={() => p.character_id && onSelect?.(p.character_id)}
            disabled={empty}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border transition-colors"
            style={{
              borderColor: highlight ? 'var(--color-accent)' : 'var(--color-border)',
              background: active ? 'var(--color-accent)' : 'transparent',
              color: active ? '#fff' : p.is_mine ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
              opacity: empty ? 0.6 : 1,
            }}
            title={empty ? '空席 · 等待真人加入' : p.role === 'ai' ? 'AI 队友 — 点击查看角色卡' : p.is_mine ? '你 — 点击查看角色卡' : '真人玩家 — 点击查看角色卡'}
          >
            <span style={{ fontSize: '0.65rem' }}>{icon}</span>
            {empty ? `空席 ${p.seat_order}` : (p.character_name || '未知角色')}
            {p.is_mine && !empty ? '（我）' : ''}
          </button>
        )
      })}
    </div>
  )
}
