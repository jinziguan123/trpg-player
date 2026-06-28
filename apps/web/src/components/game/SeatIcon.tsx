import { GiCharacter, GiRobotGolem, GiCrown, GiRockingChair } from 'react-icons/gi'

export type SeatKind = 'empty' | 'ai' | 'host' | 'me' | 'human' | 'npc'

/** 由参与者推断身份种类（房主优先于"我"，"我"优先于普通真人）。 */
export function seatKind(p: {
  character_id?: string | null
  role: string
  is_host?: boolean
  is_mine?: boolean
}): SeatKind {
  if (!p.character_id) return 'empty'
  if (p.role === 'ai') return 'ai'
  if (p.is_host) return 'host'
  if (p.is_mine) return 'me'
  return 'human'
}

/** 统一的席位/身份图标（game-icons 风格，与"返回列表"等一致）。npc 返回空。 */
export function SeatIcon({ kind, size = 14 }: { kind: SeatKind; size?: number }) {
  switch (kind) {
    case 'empty':
      return <GiRockingChair size={size} color="var(--color-text-secondary)" />
    case 'ai':
      return <GiRobotGolem size={size} color="#6c8cff" />
    case 'host':
      return <GiCrown size={size} color="#e0a82e" />
    case 'me':
      return <GiCharacter size={size} color="var(--color-accent)" />
    case 'human':
      return <GiCharacter size={size} color="var(--color-text-secondary)" />
    default:
      return null
  }
}
