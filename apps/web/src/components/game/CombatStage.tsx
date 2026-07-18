// 战斗面板主容器：顶部条（轮次徽章/收起）+ 结算回显 + 先攻轨 + 方格战场 + 参战方卡片
// + 反应/两段掷伤害/主动动作栏 + 战斗日志抽屉。子组件拆在 ./combat/ 目录。
// 两种布局：inline（聊天流底部的嵌入卡片，现状）/ immersive（沉浸战场：棋盘居中放大、
// 先攻轨横贯顶部、己方卡列左、敌方卡列右、动作区钉底）。交互逻辑与 API 契约两种布局完全共用。
import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '../../api/client'
import { GiContract, GiCrossedSwords, GiExpand, GiRollingDices, GiRun, GiScrollUnfurled } from 'react-icons/gi'
import { ChevronDown, ChevronRight } from 'lucide-react'
import type { CombatLogEntry, CombatResultView, CombatState, Combatant, PendingReaction } from './combat/types'
import { ACTIONS, REACTION_META, UNARMED, WEAPON_OTHER, isOut, type ActionKey } from './combat/meta'
import { useHpDiff } from './combat/useHpDiff'
import { CombatantCard } from './combat/CombatantCard'
import { CombatResultReveal } from './combat/CombatResultReveal'
import { InitiativeTrack } from './combat/InitiativeTrack'
import { CombatGrid } from './combat/CombatGrid'
import { TurnBanner, useTurnBanner } from './combat/TurnBanner'

// 类型 re-export：GameSessionPage / liveState.ts 的类型 import 走这里，契约不变。
export type {
  CombatState, CombatGridInfo, PendingRoll, PendingReaction,
  CombatLogEntry, CombatResultView, Combatant, CombatStatus,
} from './combat/types'

export function CombatStage({ combat, myCharId, sessionId, pendingReaction, log, result, myWeapons = [], layout = 'inline', onToggleLayout }: {
  combat: CombatState
  myCharId: string | null
  sessionId: string
  pendingReaction?: PendingReaction | null
  log: CombatLogEntry[]
  result?: CombatResultView | null   // 本场最近一次结算（掷骰落定后钉在面板顶，不必收起面板去看）
  myWeapons?: { name: string; dam?: string }[]
  layout?: 'inline' | 'immersive'    // inline=聊天流底部嵌入卡片；immersive=沉浸战场（棋盘居中放大）
  onToggleLayout?: () => void        // 布局切换回调；未传则不显示切换按钮（如窄视口）
}) {
  const immersive = layout === 'immersive'
  const order = combat.order
  const diffs = useHpDiff(order)
  const banner = useTurnBanner(combat.round, combat.turn, order, myCharId)

  // 分栏：己方（player/ally）与敌方（enemy）。
  const allies = useMemo(() => order.filter((c) => c.side !== 'enemy'), [order])
  const enemies = useMemo(() => order.filter((c) => c.side === 'enemy'), [order])
  // 可作为目标的存活候选（按动作切己方/敌方）。
  const enemyTargets = useMemo(() => enemies.filter((c) => !isOut(c)), [enemies])
  const woundedAllies = useMemo(
    () => allies.filter((c) => !isOut(c) && (c.hp < c.max_hp || c.status !== 'ok')),
    [allies],
  )

  // 当前轮到谁 + 是否轮到我。
  const active = order.find((c) => c.id === combat.turn) || null
  const me = myCharId ? order.find((c) => c.id === myCharId) || null : null
  const myTurn = !!(active && myCharId && active.id === myCharId && active.is_human)
  const iAmGrappled = !!(me && (me.conditions || []).includes('grappled'))
  const iAmBurning = !!(me && (me.conditions || []).includes('burning'))

  const [action, setAction] = useState<ActionKey>('attack')
  const [targetId, setTargetId] = useState<string>('')
  const [weaponSel, setWeaponSel] = useState<string>(UNARMED)
  const [weaponCustom, setWeaponCustom] = useState<string>('')
  const [fireMode, setFireMode] = useState<'single' | 'burst' | 'sweep'>('single')   // 连发射击模式
  const [submitting, setSubmitting] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [logOpen, setLogOpen] = useState(false)
  // 方格移动模式：'move' 常规（⌈mov/2⌉，可再攻击）/ 'dash' 冲刺（满 mov，独占回合）/ 'none'
  const [moveMode, setMoveMode] = useState<'none' | 'move' | 'dash'>('none')
  useEffect(() => { setMoveMode('none') }, [combat.turn])   // 回合切换 → 退出移动模式

  // 沉浸布局棋盘自适应格长：按中央区域可用宽高取 min，夹在 40~64px（嵌入基准 46，紧凑视口
  // 允许略缩到 40）；仍放不下时交给 overflow 滚动。ResizeObserver 跟随窗口/侧栏折叠实时重算。
  const boardAreaRef = useRef<HTMLDivElement>(null)
  const [fitCell, setFitCell] = useState(56)
  const gridCols = combat.grid?.cols ?? 0
  const gridRows = combat.grid?.rows ?? 0
  useEffect(() => {
    if (!immersive || !gridCols || !gridRows) return
    const el = boardAreaRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const r = el.getBoundingClientRect()
      // 预留：横向滚动条余量 12px；纵向「战场」标签 + 移动按钮行约 40px
      const c = Math.floor(Math.min((r.width - 12) / gridCols, (r.height - 40) / gridRows))
      setFitCell(Math.max(40, Math.min(64, c)))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [immersive, gridCols, gridRows])

  // 武器下拉项：拳头置顶（永远有）+ 角色卡武器栏（去重、保留伤害提示）+ 其它(手填)。
  const weaponOptions = useMemo(() => {
    const seen = new Set<string>([UNARMED])
    const opts: { value: string; label: string }[] = [{ value: UNARMED, label: UNARMED }]
    for (const w of myWeapons) {
      const name = (w.name || '').trim()
      if (!name || seen.has(name)) continue
      seen.add(name)
      opts.push({ value: name, label: w.dam ? `${name}（${w.dam}）` : name })
    }
    return opts
  }, [myWeapons])
  // 角色卡换人/武器栏变化后，若当前所选武器已不在列表则回落拳头。
  useEffect(() => {
    if (weaponSel !== WEAPON_OTHER && !weaponOptions.some((o) => o.value === weaponSel)) {
      setWeaponSel(UNARMED)
    }
  }, [weaponOptions, weaponSel])

  // 当前动作的目标候选（按 target 类型）。
  const actionMeta = ACTIONS[action]
  const candidates = actionMeta.target === 'enemy' ? enemyTargets : actionMeta.target === 'ally' ? woundedAllies : []
  const effectiveTarget = candidates.some((c) => c.id === targetId) ? targetId : (candidates[0]?.id ?? '')

  type ActionBody = { type: string; target_id?: string; weapon?: string; kind?: string; defense?: string; shots?: string[] }
  const curWeapon = weaponSel === WEAPON_OTHER ? weaponCustom.trim() : weaponSel
  // 火器名匹配（连射仅火器可用；最终由后端按武器射速 round 校验、非火器自动降级单发）
  const isFirearm = /枪|步枪|左轮|冲锋|自动手枪|马格南|来复|卡宾|霰弹|散弹|沙漠之鹰|格洛克|贝瑞塔|鲁格/.test(curWeapon)
  const submit = async (body: ActionBody) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/action`, body)
      // 成功后不手动刷新——后端会经 /live 广播新 combat_state。
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '行动提交失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)   // 防连点；广播的新态会重置回合归属
    }
  }

  const runAction = () => {
    if (actionMeta.target !== 'none' && !effectiveTarget) {
      toast.error(actionMeta.target === 'enemy' ? '没有可选的目标' : '没有需要处理的己方目标')
      return
    }
    switch (action) {
      case 'attack': {
        if (!curWeapon) { toast.error('请填写武器'); return }
        if (isFirearm && fireMode === 'burst') {
          // 对同一目标连开 3 发（同目标不加惩罚骰）
          void submit({ type: 'attack', weapon: curWeapon, shots: [effectiveTarget, effectiveTarget, effectiveTarget] })
        } else if (isFirearm && fireMode === 'sweep') {
          // 扫射：主目标 + 与主目标相邻（成排）的其它存活敌人各 1 发（换目标累加惩罚骰）。
          // 有坐标时按方格相邻收窄；无坐标（旧战斗态）回落到全体敌人。
          const primPos = candidates.find((c) => c.id === effectiveTarget)?.pos
          const swept = primPos
            ? candidates.filter((c) => c.id === effectiveTarget
                || (c.pos && Math.max(Math.abs(c.pos.x - primPos.x), Math.abs(c.pos.y - primPos.y)) <= 1)).map((c) => c.id)
            : candidates.map((c) => c.id)
          if (swept.length >= 2) void submit({ type: 'attack', weapon: curWeapon, shots: swept })
          else void submit({ type: 'attack', target_id: effectiveTarget, weapon: curWeapon })
        } else {
          void submit({ type: 'attack', target_id: effectiveTarget, weapon: curWeapon })
        }
        break
      }
      case 'first_aid':
        void submit({ type: 'first_aid', target_id: effectiveTarget })
        break
      case 'observe':
        void submit({ type: 'observe' })
        break
      case 'grapple':
        void submit({ type: 'maneuver', target_id: effectiveTarget, kind: 'grapple' })
        break
      case 'disarm':
        void submit({ type: 'maneuver', target_id: effectiveTarget, kind: 'disarm' })
        break
      case 'reload':
        void submit({ type: 'reload' })
        break
      case 'aim':
        void submit({ type: 'aim' })
        break
      case 'extinguish':
        void submit({ type: 'extinguish' })
        break
      case 'flee':
        void submit({ type: 'flee' })
        break
    }
  }

  // 方格移动：点可达格 → 提交移动/冲刺，成功后退出移动模式。
  const doMove = async (x: number, y: number) => {
    if (submitting || moveMode === 'none') return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/action`, { type: moveMode, dest: { x, y } })
      setMoveMode('none')
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '移动失败')
    } finally {
      setTimeout(() => setSubmitting(false), 400)
    }
  }
  // 点棋子：非移动模式下点敌方棋子 = 选为攻击目标（与目标下拉双向同步）。
  const onPieceClick = (c: Combatant) => {
    if (moveMode !== 'none') return
    if (c.side === 'enemy' && !isOut(c)) { setAction('attack'); setTargetId(c.id) }
  }

  // 两段式投骰：我攻击命中后要亲自掷伤害。
  const pendingRoll = combat.pending_roll || null
  const rollForMe = !!(pendingRoll && myCharId && pendingRoll.actor_id === myCharId)
  const rollActorName = pendingRoll ? (order.find((c) => c.id === pendingRoll.actor_id)?.name ?? '……') : ''
  const rollDamage = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/roll`, {})
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '投掷失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)
    }
  }

  // 反应提示：NPC 攻击我时，提交 fight_back/dodge/cover。
  const reactionForMe = !!(pendingReaction && myCharId && pendingReaction.defender_id === myCharId)
  const reactAs = async (choice: string) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await api.post(`/sessions/${sessionId}/combat/reaction`, { choice })
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '反应提交失败')
    } finally {
      setTimeout(() => setSubmitting(false), 600)
    }
  }


  // ===== 共享 JSX 片段（两种布局复用同一份交互逻辑，只在摆放上分叉） =====

  // 布局切换按钮（gi 图标）：inline→沉浸战场 / immersive→嵌入面板。窄视口不传回调则不渲染。
  const layoutToggleBtn = onToggleLayout && (
    <button
      onClick={onToggleLayout}
      className="flex-shrink-0"
      style={{ color: 'var(--color-text-secondary)' }}
      title={immersive ? '退出沉浸战场，回到聊天流嵌入面板' : '切换沉浸战场：棋盘居中放大，聊天收成侧栏'}
    >
      {immersive ? <GiContract size={15} /> : <GiExpand size={15} />}
    </button>
  )

  // B1.5 方格战场：战场标签 + 移动/冲刺按钮 + 棋盘。cellPx 控制格长（沉浸布局自适应放大）。
  const gridSection = (cellPx: number) => combat.grid && (
    <div className={immersive ? 'flex flex-col min-h-0' : 'mt-2'}>
      <div className="flex items-center justify-between mb-0.5 flex-shrink-0">
        <span className="text-[10px] uppercase tracking-wide" style={{ color: 'var(--color-text-secondary)' }}>战场</span>
        {myTurn && (
          <div className="flex items-center gap-1">
            <button
              onClick={() => setMoveMode((m) => (m === 'move' ? 'none' : 'move'))}
              disabled={submitting}
              title={`常规移动 ⌈mov/2⌉=${me?.move_left ?? 0} 格，移动后仍可攻击`}
              className="text-[11px] px-2 py-0.5 rounded inline-flex items-center gap-1"
              style={{
                border: `1px solid ${moveMode === 'move' ? 'var(--color-accent)' : 'var(--color-border-strong)'}`,
                color: moveMode === 'move' ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
                ...(submitting ? { opacity: 0.5 } : {}),
              }}
            >
              <GiRun size={12} /> {moveMode === 'move' ? `移动中（${me?.move_left ?? 0} 格）` : '移动'}
            </button>
            <button
              onClick={() => setMoveMode((m) => (m === 'dash' ? 'none' : 'dash'))}
              disabled={submitting}
              title={`冲刺满 mov=${me?.mov ?? 0} 格，但独占本回合（不能再攻击）`}
              className="text-[11px] px-2 py-0.5 rounded inline-flex items-center gap-1"
              style={{
                border: `1px solid ${moveMode === 'dash' ? 'var(--color-danger)' : 'var(--color-border-strong)'}`,
                color: moveMode === 'dash' ? 'var(--color-danger)' : 'var(--color-text-secondary)',
                ...(submitting ? { opacity: 0.5 } : {}),
              }}
            >
              <GiRun size={12} /> {moveMode === 'dash' ? `冲刺中（${me?.mov ?? 0} 格）` : '冲刺'}
            </button>
          </div>
        )}
      </div>
      <CombatGrid
        grid={combat.grid}
        order={order}
        turn={combat.turn}
        myCharId={myCharId}
        moveActive={moveMode !== 'none'}
        budget={moveMode === 'dash' ? (me?.mov ?? 0) : (me?.move_left ?? 0)}
        dash={moveMode === 'dash'}
        targetId={effectiveTarget}
        fx={diffs}
        onCellMove={doMove}
        onPieceClick={onPieceClick}
        cell={cellPx}
      />
    </div>
  )

  // B2 参战方卡片列（我方 / 敌方），带 HP 动画（B3）。
  const sideCol = (label: string, list: Combatant[], alignRight = false) => (
    <div className="flex flex-col gap-1.5">
      <div className={`text-[10px] uppercase tracking-wide${alignRight ? ' text-right' : ''}`} style={{ color: 'var(--color-text-secondary)' }}>{label}</div>
      {list.length === 0
        ? <div className={`text-[11px]${alignRight ? ' text-right' : ''}`} style={{ color: 'var(--color-text-secondary)' }}>无</div>
        : list.map((c) => (
          <CombatantCard key={c.id} c={c} mine={!!(myCharId && c.id === myCharId)} active={c.id === combat.turn} diff={diffs[c.id]} />
        ))}
    </div>
  )

  // B4 反应提示 / B5 主动动作栏 / 等待提示（两种布局都钉在舞台底部）。
  const actionZone = (
    <div className="mt-2 pt-2" style={{ borderTop: '1px solid var(--color-border)' }}>
        {pendingRoll ? (
          rollForMe ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs" style={{ color: 'var(--color-text-primary)' }}>
                命中！{pendingRoll.label || '投掷伤害'}
              </span>
              <button
                onClick={() => void rollDamage()}
                disabled={submitting}
                className="btn-primary text-xs !px-2.5 !py-1 flex items-center gap-1"
                style={submitting ? { opacity: 0.5 } : undefined}
              >
                <GiRollingDices size={13} /> {pendingRoll.label || '投掷伤害'}
              </button>
            </div>
          ) : (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              等待 {rollActorName} 投掷伤害…
            </div>
          )
        ) : pendingReaction ? (
          reactionForMe ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-xs" style={{ color: 'var(--color-text-primary)' }}>
                {pendingReaction.attacker_name} 用 {pendingReaction.weapon} 攻击你，如何应对？
              </span>
              <div className="flex flex-wrap items-center gap-2">
                {pendingReaction.allowed.map((choice) => {
                  const meta = REACTION_META[choice]
                  if (!meta) return null
                  const { label, Icon } = meta
                  return (
                    <button
                      key={choice}
                      onClick={() => void reactAs(choice)}
                      disabled={submitting}
                      className={`${choice === 'fight_back' ? 'btn-primary' : 'btn-secondary'} text-xs !px-2.5 !py-1 flex items-center gap-1`}
                      style={submitting ? { opacity: 0.5 } : undefined}
                    >
                      <Icon size={13} /> {label}
                    </button>
                  )
                })}
              </div>
            </div>
          ) : (
            <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
              等待 {pendingReaction.defender_name} 反应…
            </div>
          )
        ) : myTurn ? (
          <div className="flex flex-col gap-2">
            {/* 动作选择：一排 gi 图标按钮 */}
            <div className="flex flex-wrap items-center gap-1.5">
              {(Object.keys(ACTIONS) as ActionKey[]).map((k) => {
                // 被擒抱时隐藏逃跑（无效）：擒抱状态下逃跑要先挣脱，暂不给该入口。
                if (k === 'flee' && iAmGrappled) return null
                // 灭火仅在自己着火时出现（否则是无效动作）。
                if (k === 'extinguish' && !iAmBurning) return null
                const { label, Icon } = ACTIONS[k]
                const on = action === k
                return (
                  <button
                    key={k}
                    onClick={() => setAction(k)}
                    className="text-xs !px-2 !py-1 rounded inline-flex items-center gap-1 transition-colors"
                    style={{
                      border: on ? '1px solid var(--color-accent)' : '1px solid var(--color-border)',
                      color: on ? 'var(--color-text-accent)' : 'var(--color-text-secondary)',
                      background: on ? 'var(--color-bg-tertiary)' : 'transparent',
                    }}
                  >
                    <Icon size={13} /> {label}
                  </button>
                )
              })}
            </div>
            {/* 参数：目标（按动作切候选）+ 武器（仅攻击） */}
            <div className="flex flex-wrap items-end gap-2">
              {actionMeta.target !== 'none' && (
                <label className="flex flex-col gap-1">
                  <span className="combat-field-label">
                    {actionMeta.target === 'enemy' ? '目标（敌方）' : '目标（己方）'}
                  </span>
                  <div className="combat-select-wrap">
                    <select
                      className="combat-select"
                      value={effectiveTarget}
                      onChange={(e) => setTargetId(e.target.value)}
                      disabled={candidates.length === 0}
                    >
                      {candidates.length === 0 && <option value="">{actionMeta.target === 'enemy' ? '无存活敌方' : '无需处理'}</option>}
                      {candidates.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}（{c.hp}/{c.max_hp}）</option>
                      ))}
                    </select>
                    <ChevronDown className="combat-select-caret" size={13} />
                  </div>
                </label>
              )}
              {action === 'attack' && (
                <>
                  <label className="flex flex-col gap-1">
                    <span className="combat-field-label">武器</span>
                    <div className="combat-select-wrap">
                      <select
                        className="combat-select"
                        value={weaponSel}
                        onChange={(e) => setWeaponSel(e.target.value)}
                      >
                        {weaponOptions.map((o) => (<option key={o.value} value={o.value}>{o.label}</option>))}
                        <option value={WEAPON_OTHER}>其它（手填）</option>
                      </select>
                      <ChevronDown className="combat-select-caret" size={13} />
                    </div>
                  </label>
                  {weaponSel === WEAPON_OTHER && (
                    <input
                      className="input !py-1 text-xs"
                      style={{ width: 140 }}
                      placeholder="填写武器…"
                      value={weaponCustom}
                      onChange={(e) => setWeaponCustom(e.target.value)}
                      autoFocus
                    />
                  )}
                  {isFirearm && (
                    <label className="flex flex-col gap-1">
                      <span className="combat-field-label">射击</span>
                      <div className="combat-select-wrap">
                        <select
                          className="combat-select"
                          value={fireMode}
                          onChange={(e) => setFireMode(e.target.value as 'single' | 'burst' | 'sweep')}
                        >
                          <option value="single">单发</option>
                          <option value="burst">连射3发（同目标）</option>
                          <option value="sweep" disabled={candidates.length < 2}>扫射（每敌1发）</option>
                        </select>
                        <ChevronDown className="combat-select-caret" size={13} />
                      </div>
                    </label>
                  )}
                </>
              )}
              <button
                onClick={runAction}
                disabled={submitting}
                className={`${action === 'flee' ? 'btn-secondary' : 'btn-primary'} text-xs !px-2.5 !py-1 flex items-center gap-1`}
                style={{
                  ...(submitting ? { opacity: 0.5 } : {}),
                  ...(action === 'flee' ? { color: 'var(--color-danger)' } : {}),
                }}
              >
                <actionMeta.Icon size={13} /> {actionMeta.label}
              </button>
            </div>
          </div>
        ) : (
          <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            战斗中 · 轮到 {active?.name ?? '……'}
          </div>
        )}
    </div>
  )

  // B6 折叠战斗日志抽屉。
  const logDrawer = log.length > 0 && (
        <div className="mt-2 pt-2 flex-shrink-0" style={{ borderTop: '1px solid var(--color-border)' }}>
          <button
            onClick={() => setLogOpen((v) => !v)}
            className="flex items-center gap-1.5 text-xs w-full"
            style={{ color: 'var(--color-text-secondary)' }}
            title={logOpen ? '收起战斗日志' : '展开战斗日志'}
          >
            {logOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <GiScrollUnfurled size={13} style={{ color: 'var(--color-text-accent)' }} />
            <span style={{ color: 'var(--color-text-accent)' }}>战斗日志 ({log.length})</span>
          </button>
          {logOpen && (
            <div className="mt-1.5 flex flex-col gap-1 max-h-56 overflow-y-auto chat-scroll">
              {log.map((e) => (
                <div
                  key={e.id}
                  className="text-[11px] px-2 py-1 rounded"
                  style={{
                    background: 'var(--color-bg-secondary)',
                    border: '1px solid var(--color-border)',
                    color: e.kind === 'dice' ? 'var(--color-dice-gold)' : 'var(--color-text-secondary)',
                    fontFamily: e.kind === 'dice' ? 'var(--font-body)' : undefined,
                  }}
                >
                  {e.content}
                </div>
              ))}
            </div>
          )}
        </div>
  )

  // ===== 沉浸布局：战场为主（棋盘居中放大、先攻轨横贯顶、参战卡列环绕、动作区钉底） =====
  if (immersive) {
    return (
      <div className="card combat-hud combat-stage--immersive !px-3 !py-2.5">
        {/* 顶部条：战斗标识 + 轮次徽章 + 当前行动者 + 布局切换 */}
        <div className="flex items-center gap-2 mb-2 flex-shrink-0">
          <GiCrossedSwords style={{ color: 'var(--color-danger)', fontSize: '1.05rem', flexShrink: 0 }} />
          <span className="text-sm font-semibold flex-shrink-0" style={{ color: 'var(--color-text-accent)' }}>战斗</span>
          <span className="combat-round-badge flex-shrink-0" title={`当前第 ${combat.round} 轮`}>
            第 <span className="combat-round-badge-num">{combat.round}</span> 轮
          </span>
          <span className="text-xs truncate" style={{ color: 'var(--color-text-secondary)' }}>· 轮到 {active?.name ?? '……'}</span>
          <div className="ml-auto flex items-center gap-2">{layoutToggleBtn}</div>
        </div>
        {/* 结算回显：钉在舞台顶部 */}
        {result && <div className="flex-shrink-0"><CombatResultReveal result={result} order={order} /></div>}
        {/* B1 先攻轨：横贯顶部 */}
        <div className="flex-shrink-0">
          <InitiativeTrack order={order} turn={combat.turn} myCharId={myCharId} />
        </div>
        {/* 中段：己方卡列（左）| 棋盘居中放大 | 敌方卡列（右）；回合横幅覆盖顶部 */}
        <div className="relative flex-1 min-h-0 flex items-stretch gap-3 mt-1">
          <TurnBanner banner={banner} />
          <div className="w-40 flex-shrink-0 overflow-y-auto chat-scroll">
            {sideCol('我方', allies)}
          </div>
          <div ref={boardAreaRef} className="flex-1 min-w-0 flex flex-col justify-center overflow-auto chat-scroll">
            {combat.grid
              ? gridSection(fitCell)
              : <div className="text-xs text-center py-6" style={{ color: 'var(--color-text-secondary)' }}>本场战斗没有方格战场</div>}
          </div>
          <div className="w-40 flex-shrink-0 overflow-y-auto chat-scroll">
            {sideCol('敌方', enemies, true)}
          </div>
        </div>
        {/* 动作栏/反应区/掷伤害 + 战斗日志：钉在舞台底部 */}
        <div className="flex-shrink-0">{actionZone}</div>
        {logDrawer}
      </div>
    )
  }

  // ===== 嵌入布局（现状）：聊天流底部的可收起卡片 =====
  return (
    <div className="card combat-hud mx-3 mb-2 !px-3 !py-2.5">
      {/* 顶部：战斗标识 + 轮次徽章 + 布局切换 + 收起/展开 */}
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <GiCrossedSwords style={{ color: 'var(--color-danger)', fontSize: '1.05rem', flexShrink: 0 }} />
          <span className="text-sm font-semibold flex-shrink-0" style={{ color: 'var(--color-text-accent)' }}>战斗</span>
          <span className="combat-round-badge flex-shrink-0" title={`当前第 ${combat.round} 轮`}>
            第 <span className="combat-round-badge-num">{combat.round}</span> 轮
          </span>
          {collapsed && (
            <span className="text-xs truncate" style={{ color: 'var(--color-text-secondary)' }}>· 轮到 {active?.name ?? '……'}</span>
          )}
          {collapsed && (myTurn || rollForMe || reactionForMe) && (
            <span className="text-[10px] px-1 rounded flex-shrink-0" style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-accent)' }}>待你操作</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {layoutToggleBtn}
          <button
            onClick={() => setCollapsed((v) => !v)}
            className="flex-shrink-0"
            style={{ color: 'var(--color-text-secondary)' }}
            title={collapsed ? '展开战斗面板' : '收起战斗面板'}
          >
            {collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>
      {/* 结算回显：掷骰落定后钉在面板顶（含收起态），无需收面板即可看到本次成败 / 对抗双方数值 */}
      {result && <CombatResultReveal result={result} order={order} />}
      {!collapsed && (<>
      {/* B1 先攻轨：横排，高亮当前、标下一个、走过者淡化 */}
      <InitiativeTrack order={order} turn={combat.turn} myCharId={myCharId} />

      {/* 战场区（相对定位承载回合/轮次横幅覆盖层） */}
      <div className="relative">
        <TurnBanner banner={banner} />
        {/* B1.5 方格战场：令牌 + 移动。移动模式仅本人回合可用。 */}
        {gridSection(46)}
        {/* B2 两栏参战方卡片（左己方 / 右敌方） */}
        <div className="grid grid-cols-2 gap-2 mt-2">
          {sideCol('我方', allies)}
          {sideCol('敌方', enemies, true)}
        </div>
      </div>

      {actionZone}
      {logDrawer}
      </>)}
    </div>
  )
}
