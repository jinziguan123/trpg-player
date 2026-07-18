// 战斗面板动画驱动 hooks：HP diff / 出局 diff / prefers-reduced-motion。
import { useEffect, useRef, useState } from 'react'
import type { Combatant, HpDiff } from './types'
import { isOut } from './meta'

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
}

// 响应式 reduced-motion：给内联 transition/动画开关用（CSS 类另有 @media 兜底）。
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion)
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!mq) return
    const on = () => setReduced(mq.matches)
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])
  return reduced
}

// —— HP 变化动画驱动：记住上一帧各 id 的 hp，新态到达时 diff ——
// 返回每个 id 的 { delta, seq }：delta<0 掉血、>0 回血、0 无变化；seq 让同值连续变化也能重触发动画。
// 首次见到某 id 时只建基准、不产出 delta（防重连把满血误判为回血）。
export function useHpDiff(order: Combatant[]): Record<string, HpDiff> {
  const prevHp = useRef<Map<string, number>>(new Map())
  const seqRef = useRef(0)
  const [diffs, setDiffs] = useState<Record<string, HpDiff>>({})

  useEffect(() => {
    const reduced = prefersReducedMotion()
    const next: Record<string, HpDiff> = {}
    const seen = new Set<string>()
    for (const c of order) {
      seen.add(c.id)
      const before = prevHp.current.get(c.id)
      if (before === undefined) {
        // 首次见到：只建基准，不动画（重连首帧不误判为回血）。
        prevHp.current.set(c.id, c.hp)
        continue
      }
      if (!reduced && c.hp !== before) {
        seqRef.current += 1
        next[c.id] = { delta: c.hp - before, seq: seqRef.current }
      }
      prevHp.current.set(c.id, c.hp)
    }
    // 清掉已离场 id 的基准（避免同 id 复用时错乱）。
    for (const id of Array.from(prevHp.current.keys())) if (!seen.has(id)) prevHp.current.delete(id)
    if (Object.keys(next).length > 0) setDiffs((prev) => ({ ...prev, ...next }))
  }, [order])

  return diffs
}

// —— 出局（死亡/逃离）瞬间检测：进入 out 态的那一帧产出 seq，驱动令牌的一次性灰化缩小动画 ——
// 口径与 useHpDiff 一致：首帧只建基准（重连时已死者不再重播死亡动画）。
export function useDeathFx(order: Combatant[]): Record<string, number> {
  const prevOut = useRef<Map<string, boolean>>(new Map())
  const seqRef = useRef(0)
  const [fx, setFx] = useState<Record<string, number>>({})

  useEffect(() => {
    const reduced = prefersReducedMotion()
    const next: Record<string, number> = {}
    const seen = new Set<string>()
    for (const c of order) {
      seen.add(c.id)
      const before = prevOut.current.get(c.id)
      const now = isOut(c)
      if (before === undefined) { prevOut.current.set(c.id, now); continue }
      if (!reduced && now && !before) {
        seqRef.current += 1
        next[c.id] = seqRef.current
      }
      prevOut.current.set(c.id, now)
    }
    for (const id of Array.from(prevOut.current.keys())) if (!seen.has(id)) prevOut.current.delete(id)
    if (Object.keys(next).length > 0) setFx((prev) => ({ ...prev, ...next }))
  }, [order])

  return fx
}
