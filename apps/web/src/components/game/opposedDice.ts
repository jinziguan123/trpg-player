export interface OpposedSide {
  name: string
  roll: number
  target: number
  skill: string
  outcome: string
}

export interface OpposedData {
  attacker: OpposedSide
  defender: OpposedSide | null
  winner: 'attacker' | 'defender' | null
  result: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function normalizeSide(value: unknown): OpposedSide | null {
  if (!isRecord(value)) return null
  const name = String(value.name ?? value.actor ?? '').trim()
  const skill = String(value.skill ?? '').trim()
  const roll = Number(value.roll)
  const target = Number(value.target)
  if (!name || !skill || !Number.isFinite(roll) || !Number.isFinite(target)) return null
  return {
    name,
    skill,
    roll,
    target,
    outcome: String(value.outcome ?? ''),
  }
}

/** 兼容战斗对抗对象、新版手动对抗对象，以及旧版 opposed=true + a/b 平铺事件。 */
export function normalizeOpposedData(metadata: Record<string, unknown> | undefined): OpposedData | null {
  if (!metadata) return null
  const raw = isRecord(metadata.opposed) ? metadata.opposed : metadata
  const attacker = normalizeSide(raw.attacker ?? raw.a ?? metadata.a)
  const defender = normalizeSide(raw.defender ?? raw.b ?? metadata.b)
  if (!attacker) return null

  const rawWinner = String(raw.winner ?? metadata.winner ?? '')
  let winner: OpposedData['winner'] = null
  if (rawWinner === 'attacker' || rawWinner === attacker.name) winner = 'attacker'
  else if (defender && (rawWinner === 'defender' || rawWinner === defender.name)) winner = 'defender'

  const result = String(raw.result ?? metadata.result ?? '').trim()
    || (winner === 'attacker'
      ? `${attacker.name} 胜`
      : winner === 'defender' && defender
        ? `${defender.name} 胜`
        : '平局')
  return { attacker, defender, winner, result }
}
