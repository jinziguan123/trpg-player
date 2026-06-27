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
        const isPrimary = p.is_primary
        const active = selectedId === p.character_id
        return (
          <button
            key={p.character_id}
            onClick={() => onSelect?.(p.character_id)}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border transition-colors"
            style={{
              borderColor: active || isPrimary ? 'var(--color-accent)' : 'var(--color-border)',
              background: active ? 'var(--color-accent)' : 'transparent',
              color: active ? '#fff' : isPrimary ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
            }}
            title={isPrimary ? '主角（你）— 点击查看角色卡' : 'AI 队友 — 点击查看角色卡'}
          >
            <span style={{ fontSize: '0.65rem' }}>{isPrimary ? '★' : '🤖'}</span>
            {p.character_name || '未知角色'}
          </button>
        )
      })}
    </div>
  )
}
