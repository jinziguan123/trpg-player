import type { CombatState, PendingReaction, PendingRoll } from '@/components/game/CombatStage'
import type { ChaseState } from '@/components/game/ChasePanel'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isCombatant(value: unknown): value is CombatState['order'][number] {
  if (!isRecord(value)) return false
  return typeof value.id === 'string'
    && typeof value.name === 'string'
    && (value.side === 'player' || value.side === 'ally' || value.side === 'enemy')
    && typeof value.is_human === 'boolean'
    && typeof value.hp === 'number'
    && typeof value.max_hp === 'number'
    && typeof value.status === 'string'
}

export function parseCombatState(value: unknown): CombatState | null {
  if (!isRecord(value)) return null
  if (typeof value.round !== 'number') return null
  if (value.turn !== null && typeof value.turn !== 'string') return null
  if (!Array.isArray(value.order) || !value.order.every(isCombatant)) return null
  if (value.started_seq !== undefined && typeof value.started_seq !== 'number') return null
  const pendingRoll = parsePendingRoll(value.pending_roll)
  return {
    round: value.round,
    turn: value.turn,
    order: value.order,
    ...(value.started_seq === undefined ? {} : { started_seq: value.started_seq }),
    ...(pendingRoll ? { pending_roll: pendingRoll } : {}),
  }
}

function parsePendingRoll(value: unknown): PendingRoll | null {
  if (!isRecord(value)) return null
  if (typeof value.actor_id !== 'string' || typeof value.kind !== 'string') return null
  return {
    actor_id: value.actor_id,
    kind: value.kind,
    label: typeof value.label === 'string' ? value.label : '投掷',
    ...(typeof value.victim_id === 'string' ? { victim_id: value.victim_id } : {}),
  }
}

export function parsePendingReaction(value: unknown): PendingReaction | null {
  if (!isRecord(value)) return null
  if (typeof value.attacker_id !== 'string' || typeof value.defender_id !== 'string') return null
  if (typeof value.weapon !== 'string' || typeof value.ranged !== 'boolean') return null
  if (!isStringArray(value.allowed)) return null
  if (typeof value.attacker_name !== 'string' || typeof value.defender_name !== 'string') return null
  return {
    attacker_id: value.attacker_id,
    defender_id: value.defender_id,
    weapon: value.weapon,
    ranged: value.ranged,
    allowed: value.allowed,
    attacker_name: value.attacker_name,
    defender_name: value.defender_name,
  }
}

export function parseChaseState(value: unknown): ChaseState | null {
  if (!isRecord(value)) return null
  if (typeof value.round !== 'number' || typeof value.gap !== 'number') return null
  if (typeof value.escape_at !== 'number' || typeof value.caught_at !== 'number') return null
  if (typeof value.quarry !== 'string' || typeof value.pursuer !== 'string') return null
  return {
    round: value.round,
    gap: value.gap,
    escape_at: value.escape_at,
    caught_at: value.caught_at,
    quarry: value.quarry,
    pursuer: value.pursuer,
  }
}
