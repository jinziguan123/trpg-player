// 战斗面板共享类型：与后端 combat 广播的 metadata 契约对齐。
// 仅放纯类型；图标/元数据映射见 meta.ts。

// 参战方状态：与后端枚举对齐。
export type CombatStatus = 'ok' | 'major_wound' | 'dying' | 'unconscious' | 'dead' | 'fled'

export interface Combatant {
  id: string
  name: string
  side: 'player' | 'ally' | 'enemy'
  is_human: boolean
  hp: number
  max_hp: number
  status: CombatStatus
  weapon?: string         // 当前武器名（后端 order 投影透传）
  armor?: number          // 护甲值（每次物理伤害先扣它）
  conditions?: string[]   // 正交条件：grappled（被擒）/ disarmed（缴械）
  aim?: boolean           // 瞄准态（下一击加奖励骰）
  pos?: { x: number; y: number } | null   // 方格坐标
  mov?: number            // 移动力（格/轮）
  move_left?: number      // 本回合剩余移动预算
}

// 方格战场（尺寸 + 障碍/掩体）。MVP 只用 cols/rows/cell_m。
export interface CombatGridInfo {
  cols: number
  rows: number
  cell_m: number
  blocked?: string[]
  cover?: Record<string, string>
}

// 两段式投骰：真人攻击命中后，等该玩家亲自掷伤害。actor_id 为该掷骰的玩家。
export interface PendingRoll {
  actor_id: string
  kind: string          // 目前为 'damage'
  label: string         // 按钮/提示文案，如「投掷伤害（1D8+DB）」
  victim_id?: string
}

export interface CombatState {
  round: number
  turn: string | null   // 当前轮到的参战方 id
  order: Combatant[]
  started_seq?: number  // 本场战斗日志起点 seq：日志抽屉只收本场（seq>started_seq）的结算行
  pending_roll?: PendingRoll | null
  grid?: CombatGridInfo | null   // 方格战场
}

// 反应提示：NPC 攻击某真人时后端暂停并广播 combat_reaction_prompt 的 metadata。
// allowed 为 ['fight_back','dodge']（近战）或 ['dodge','cover']（火器）。
export interface PendingReaction {
  attacker_id: string
  defender_id: string
  weapon: string
  ranged: boolean
  allowed: string[]
  attacker_name: string
  defender_name: string
}

// 一条战斗日志（机械结算行）：由 GameSessionPage 从带 combat_log 的 chunk 分流而来。
export interface CombatLogEntry {
  id: string
  kind: 'dice' | 'system'
  content: string
}

// 本场最近一次结算的结构化视图：普通命中给 content+hit；对抗给双方数值/结果。
export interface CombatResultView {
  content: string
  metadata: Record<string, unknown>
}
export interface OppSide { name: string; roll: number; target: number; skill: string; outcome: string }
export interface OppData {
  attacker: OppSide
  defender: OppSide | null
  winner: 'attacker' | 'defender' | null
  result: string
}

// HP 变化 diff（useHpDiff 产出）：delta<0 掉血、>0 回血；seq 让同值连续变化也能重触发动画。
export interface HpDiff { delta: number; seq: number }
