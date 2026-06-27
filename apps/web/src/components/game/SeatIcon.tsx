import { Crown, Bot, User, Armchair } from 'lucide-react'

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

/** 统一的席位/身份图标（lucide 线条图标 + 上色，替代系统 emoji）。npc 返回空。 */
export function SeatIcon({ kind, size = 14 }: { kind: SeatKind; size?: number }) {
  const sw = 2.2
  switch (kind) {
    case 'empty':
      return <Armchair size={size} strokeWidth={sw} color="var(--color-text-secondary)" />
    case 'ai':
      return <Bot size={size} strokeWidth={sw} color="#6c8cff" />
    case 'host':
      return <Crown size={size} strokeWidth={sw} color="#e0a82e" />
    case 'me':
      return <User size={size} strokeWidth={sw} color="var(--color-accent)" />
    case 'human':
      return <User size={size} strokeWidth={sw} color="var(--color-text-secondary)" />
    default:
      return null
  }
}
