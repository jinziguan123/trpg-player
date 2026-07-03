import { useEffect, useImperativeHandle, useRef, useState, forwardRef, useCallback } from 'react'
import { specToNotation, type DiceSpec } from './diceNotation'

export type { DiceSpec } from './diceNotation'

// 3D 投掷落定后，组件在动画结束（或超时兜底）时 resolve，调用方据此再显现原有的结果卡
// （保证卡片不先于动画蹦出）。
export interface DiceRollerHandle {
  /** 播放一次预定结果的 3D 投掷。返回的 Promise 在落定/超时后 resolve。 */
  roll: (spec: DiceSpec) => Promise<void>
}

// 动画硬超时：物理没停也强制落定显示结果，绝不卡住跑团（dice-box 的落定回调在部分环境不稳）。
const ROLL_TIMEOUT_MS = 2500
// 落定后停留、让玩家看清点数的时长，再淡出交还结果卡。
const SETTLE_HOLD_MS = 600

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
}

// 主题琥珀配色：骰面暖白、点数深色，金色棱边。与哥特/羊皮纸两主题皆协调。
const AMBER_COLORSET = {
  name: 'trpg-amber',
  description: 'TRPG 琥珀',
  category: 'Colors',
  foreground: '#2a1d0a',   // 点数：深褐（暖白骰面上清晰可读）
  background: '#efe6d0',   // 骰面：暖白象牙
  outline: '#8a6a2e',      // 描边：暗琥珀
  edge: '#c99a45',         // 棱边：琥珀金
  texture: 'none',
  material: 'plastic',
}

// 本地资源根（离线桌面应用，禁 CDN）：assets 已拷到 public/dice-box/。
const ASSET_PATH = '/dice-box/'

type DiceBoxInstance = {
  initialize?: () => Promise<void>   // 构造后须 await：建 WebGL renderer / 加载主题，之后 roll 才可用
  roll: (n: string) => Promise<unknown>
  clearDice?: () => void
  dispose?: () => void
}

// 每个组件实例一个稳定的容器 id：dice-box 构造函数以 CSS 选择器（document.querySelector）定位容器。
let containerSeq = 0

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  } catch { return false }
}

export const DiceRoller = forwardRef<DiceRollerHandle, Record<string, never>>(function DiceRoller(_props, ref) {
  const containerIdRef = useRef<string>(`dice-box-container-${++containerSeq}`)
  const containerRef = useRef<HTMLDivElement>(null)
  const boxRef = useRef<DiceBoxInstance | null>(null)
  const boxReadyRef = useRef<Promise<DiceBoxInstance | null> | null>(null)  // 初始化 Promise（幂等）
  // box 的落定回调不稳，改用「当次 roll 的 resolve」由回调或超时任一先到者触发。
  const rollResolveRef = useRef<(() => void) | null>(null)
  const [active, setActive] = useState(false)   // 覆盖层是否显示（投掷进行中）

  // 惰性但幂等地初始化 dice-box：首个投掷时创建一次，Promise 缓存复用。
  // 容器始终布局占位（visibility:hidden 仍有尺寸），此时 clientWidth/Height 非零，能安全建场景。
  const ensureBox = useCallback((): Promise<DiceBoxInstance | null> => {
    if (boxReadyRef.current) return boxReadyRef.current
    boxReadyRef.current = (async () => {
      const container = containerRef.current
      if (!container || !hasWebGL()) return null
      // 让出一拍确保布局已 flush（覆盖层刚显形时尺寸可能尚未生效）。用 setTimeout（后台仍触发）。
      if (container.clientWidth === 0 || container.clientHeight === 0) {
        await new Promise((r) => setTimeout(r, 32))
      }
      if (container.clientWidth === 0 || container.clientHeight === 0) {
        boxReadyRef.current = null  // 仍无尺寸：不缓存失败，下次可重试
        return null
      }
      try {
        const mod = await import('@3d-dice/dice-box-threejs')
        const DiceBox = (mod as { default: new (sel: string, cfg: Record<string, unknown>) => DiceBoxInstance }).default
        // 传 CSS 选择器字符串（库内部 document.querySelector）——不能传 HTMLElement。
        const box = new DiceBox(`#${containerIdRef.current}`, {
          assetPath: ASSET_PATH,
          theme_customColorset: AMBER_COLORSET,
          theme_surface: 'green-felt',
          theme_material: 'plastic',
          sounds: false,
          shadows: true,
          gravity_multiplier: 400,
          baseScale: 100,
          strength: 1,
          light_intensity: 0.9,
          // 落定回调：投掷完成时触发，用它提前 resolve 当次 roll（超时是兜底）。
          onRollComplete: () => { const r = rollResolveRef.current; rollResolveRef.current = null; r?.() },
        })
        if (typeof box.initialize === 'function') await box.initialize()
        boxRef.current = box
        return box
      } catch (e) {
        console.warn('[DiceRoller] 初始化失败，降级为直接显示结果', e)
        return null
      }
    })()
    return boxReadyRef.current
  }, [])

  // 单次投掷的实际执行（不含排队）。
  const runOne = useCallback(async (spec: DiceSpec): Promise<void> => {
    if (prefersReducedMotion()) return   // reduced-motion：跳过动画，直接交还结果卡
    const notation = specToNotation(spec)
    if (!notation) return
    // 先显形覆盖层，容器完成布局后再初始化/投掷。
    setActive(true)
    // 用 setTimeout 而非 requestAnimationFrame 让出一拍：rAF 在标签页不可见时会被节流/挂起，
    // 会导致投掷流程卡死；setTimeout 在后台仍会触发。
    await new Promise((r) => setTimeout(r, 32))
    const box = await ensureBox()
    if (!box) { setActive(false); return }   // WebGL 不可用 / 加载失败 → 静默降级
    try { box.clearDice?.() } catch { /* ignore */ }   // 清掉上次残留骰子，避免叠加
    // 落定 = onRollComplete 与 硬超时 任一先到；两者都不会让流程悬挂（物理没停也强制落定）。
    await new Promise<void>((resolve) => {
      let done = false
      const finish = () => { if (!done) { done = true; resolve() } }
      rollResolveRef.current = finish
      setTimeout(finish, ROLL_TIMEOUT_MS)
      try {
        const p = box.roll(notation)
        if (p && typeof (p as Promise<unknown>).catch === 'function') (p as Promise<unknown>).catch(() => finish())
      } catch { finish() }
    })
    rollResolveRef.current = null
    await new Promise((res) => setTimeout(res, SETTLE_HOLD_MS))   // 停留让玩家看清点数
    setActive(false)
  }, [ensureBox])

  // 对外的 roll：串行排队（同一覆盖层同一时刻只播一个；避免并发投掷互相清骰/抢状态）。
  const queueRef = useRef<Promise<void>>(Promise.resolve())
  const roll = useCallback((spec: DiceSpec): Promise<void> => {
    const next = queueRef.current.then(() => runOne(spec)).catch(() => { /* 单次失败不阻塞后续 */ })
    queueRef.current = next
    return next
  }, [runOne])

  useImperativeHandle(ref, () => ({ roll }), [roll])

  // 卸载/切会话：dispose 释放 WebGL 上下文，防泄漏。
  useEffect(() => {
    const container = containerRef.current
    return () => {
      const box = boxRef.current
      try { box?.clearDice?.() } catch { /* ignore */ }
      try { box?.dispose?.() } catch { /* ignore */ }
      // 兜底：移除遗留 canvas，彻底断开上下文引用（canvas 可能挂在容器内或其子层）。
      try { container?.querySelectorAll('canvas').forEach((c) => c.remove()) } catch { /* ignore */ }
      boxRef.current = null
      boxReadyRef.current = null
      rollResolveRef.current = null
    }
  }, [])

  return (
    // 覆盖层始终布局占位（container 需非零尺寸供 dice-box 建场景），仅用 opacity/visibility 显隐；
    // 不用 display:none，否则 WebGL 画布测量到 0×0。
    <div
      aria-hidden="true"
      style={{
        position: 'absolute',
        inset: 0,
        zIndex: 40,
        pointerEvents: 'none',
        background: 'radial-gradient(ellipse at center, rgba(0,0,0,0.28) 0%, rgba(0,0,0,0.55) 100%)',
        transition: 'opacity 200ms ease',
        opacity: active ? 1 : 0,
        visibility: active ? 'visible' : 'hidden',
      }}
    >
      <div id={containerIdRef.current} ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
    </div>
  )
})
