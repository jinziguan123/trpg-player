import { useEffect, useMemo, useRef, useState } from 'react'
import { Stage, Layer, Group, RegularPolygon, Line, Rect, Circle, Text } from 'react-konva'
import type Konva from 'konva'
import type { KonvaEventObject } from 'konva/lib/Node'

/** 六边形沙盘（文明式大地图）：场景按后端 axial 坐标落格，程序化地貌瓦片 + 战争迷雾。
 *  三态迷雾：已到访=全彩；听说过未到访=蒙尘剪影+名称；未知=浓雾（仅 KP 上帝视角可见）。
 *  点已知地点 = 选中前往目标（外层沿用调查板同一套二次确认 → travel 流）。
 *  坐标是象征性相对位置（只表方位与相对远近），与后端 hex_map.py 同一套 pointy-top 投影。 */

export interface SandboxLocation {
  id: string
  name: string
  current: boolean
  visited: boolean
  connections?: string[]
  party?: string[]
  clues?: { id: string; name: string; status: string }[]
  map?: { q: number; r: number; biome: string } | null
  known?: boolean
  danger?: string
}

interface Props {
  locations: SandboxLocation[]
  disabled: boolean
  /** 点击已知地点（travel 选点）。不传则瓦片不可点（如模组页只读浏览）。 */
  onPick?: (loc: SandboxLocation) => void
  /** 编辑落位：可拖场景到新格，落格时回调（父组件负责撞格校验与状态更新）。 */
  editable?: boolean
  onMoveScene?: (id: string, q: number, r: number) => void
  height?: string
}

const R = 46                       // hex 外接圆半径（px）
const CANDLE = '#d4a24e'           // 烛光琥珀（与主题 --color-accent 同源）
const PARCH = '#e8dcc0'            // 羊皮纸
const TWINE = 'rgba(138,122,92,0.55)'  // 麻绳路径
const THREAD = '#a13a42'           // 线索红
const FOG = '#0d0b08'              // 迷雾底色

/** pointy-top axial → 像素（与后端 hex_map._to_pixel 同一取向：东 +q，南 +r 屏幕向）。 */
const SQRT3 = Math.sqrt(3)
const hexXY = (q: number, r: number) => ({ x: R * SQRT3 * (q + r / 2), y: R * 1.5 * r })

/** 像素 → 最近的 axial 格（cube 取整，与后端 _axial_round 同法），拖拽落格用。 */
function xyToHex(x: number, y: number): { q: number; r: number } {
  const rf = y / (R * 1.5)
  const qf = x / (R * SQRT3) - rf / 2
  const sf = -qf - rf
  let q = Math.round(qf), r = Math.round(rf)
  const s = Math.round(sf)
  const dq = Math.abs(q - qf), dr = Math.abs(r - rf), ds = Math.abs(s - sf)
  if (dq > dr && dq > ds) q = -r - s
  else if (dr > ds) r = -q - s
  return { q, r }
}

const DANGER_COLORS: Record<string, string> = {
  uneasy: '#cfa93f', dangerous: '#d1703c', deadly: '#c0392b',
}

const AXIAL_DIRS: [number, number][] = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]]

/** 地貌样式：底色/装饰色都按哥特暗色调定调（瓦片本身是「桌上的图版」，双主题下均成立）。 */
const BIOME: Record<string, { fill: string; deco: string }> = {
  plain: { fill: '#3a3b2e', deco: '#7a7c58' },
  forest: { fill: '#26352a', deco: '#567e5c' },
  water: { fill: '#1f3140', deco: '#547f9e' },
  coast: { fill: '#2e3a3e', deco: '#6d94a1' },
  desert: { fill: '#4a3f2a', deco: '#96805a' },
  mountain: { fill: '#3a3a42', deco: '#82828e' },
  swamp: { fill: '#2e3626', deco: '#66794e' },
  urban: { fill: '#3d3430', deco: '#8a7666' },
  ruin: { fill: '#38312f', deco: '#78685f' },
  interior: { fill: '#332c26', deco: '#6e6152' },
}
const biomeOf = (b?: string) => BIOME[(b || '').toLowerCase()] || BIOME.plain

/** (q,r) → 确定性伪随机序列：装饰群落每格固定，不随重绘抖动。 */
function rng(q: number, r: number) {
  let s = (q * 374761393 + r * 668265263) | 0
  return () => {
    s = (s ^ (s << 13)) | 0; s = (s ^ (s >>> 17)) | 0; s = (s ^ (s << 5)) | 0
    return ((s >>> 0) % 1000) / 1000
  }
}

/** 程序化地貌装饰（每 biome 一套矢量小群落；确定性、无位图素材）。 */
function HexDeco({ q, r, biome }: { q: number; r: number; biome: string }) {
  const rand = rng(q, r)
  const c = biomeOf(biome).deco
  const spots = (n: number, spread = 0.52) =>
    Array.from({ length: n }, () => ({
      x: (rand() - 0.5) * 2 * R * spread,
      y: (rand() - 0.5) * 2 * R * spread * 0.8,
    }))
  const kind = (biome || '').toLowerCase()
  if (kind === 'forest') {
    return <>{spots(4).map((p, i) => (
      <Line key={i} points={[p.x - 5, p.y + 6, p.x, p.y - 8, p.x + 5, p.y + 6]}
        closed fill={c} opacity={0.85} listening={false} />
    ))}</>
  }
  if (kind === 'water' || kind === 'coast') {
    return <>{spots(kind === 'water' ? 3 : 2, 0.4).map((p, i) => (
      <Line key={i} listening={false} stroke={c} strokeWidth={1.6} opacity={0.9} lineCap="round"
        points={[p.x - 12, p.y, p.x - 6, p.y - 3.5, p.x, p.y, p.x + 6, p.y - 3.5, p.x + 12, p.y]}
        tension={0.6} />
    ))}</>
  }
  if (kind === 'mountain') {
    return <>{spots(2, 0.35).map((p, i) => (
      <Line key={i} points={[p.x - 9, p.y + 7, p.x, p.y - 9, p.x + 9, p.y + 7]}
        closed fill={c} opacity={0.85} listening={false} />
    ))}</>
  }
  if (kind === 'urban') {
    return <>{spots(4, 0.42).map((p, i) => (
      <Rect key={i} x={p.x - 4} y={p.y - 3} width={8} height={7} fill={c}
        opacity={0.85} cornerRadius={0.5} listening={false} />
    ))}</>
  }
  if (kind === 'desert') {
    return <>{spots(7).map((p, i) => (
      <Circle key={i} x={p.x} y={p.y} radius={1.3} fill={c} opacity={0.8} listening={false} />
    ))}</>
  }
  if (kind === 'swamp') {
    return <>{spots(4, 0.45).map((p, i) => (
      <Line key={i} points={[p.x - 6, p.y, p.x + 6, p.y]} stroke={c} strokeWidth={1.6}
        opacity={0.8} lineCap="round" listening={false} />
    ))}</>
  }
  if (kind === 'ruin') {
    return <>{spots(3, 0.4).map((p, i) => (
      <Rect key={i} x={p.x - 3} y={p.y - 5} width={6} height={10} fill={c} opacity={0.8}
        rotation={(rand() - 0.5) * 30} listening={false} />
    ))}</>
  }
  if (kind === 'interior') {
    return <>{[-8, 0, 8].map((dy, i) => (
      <Line key={i} points={[-R * 0.5, dy, R * 0.5, dy]} stroke={c} strokeWidth={1}
        opacity={0.5} listening={false} />
    ))}</>
  }
  return <>{spots(5).map((p, i) => (
    <Line key={i} points={[p.x - 4, p.y, p.x + 4, p.y - 1.5]} stroke={c} strokeWidth={1.2}
      opacity={0.7} lineCap="round" listening={false} />
  ))}</>
}

export function HexSandbox({ locations, disabled, onPick, editable, onMoveScene, height = 'clamp(320px, 58vh, 560px)' }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 640, h: 420 })
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    return () => ro.disconnect()
  }, [])

  const located = useMemo(
    () => locations.filter((l) => l.map && Number.isFinite(l.map.q) && Number.isFinite(l.map.r)),
    [locations],
  )

  // 初始视野：已落格场景的包围盒居中适配（无坐标时给个空舞台兜底）。
  const fit = useMemo(() => {
    if (!located.length) return { x: size.w / 2, y: size.h / 2, scale: 1 }
    const pts = located.map((l) => hexXY(l.map!.q, l.map!.r))
    const minX = Math.min(...pts.map((p) => p.x)) - R * 2.2
    const maxX = Math.max(...pts.map((p) => p.x)) + R * 2.2
    const minY = Math.min(...pts.map((p) => p.y)) - R * 2.6
    const maxY = Math.max(...pts.map((p) => p.y)) + R * 2.6
    const scale = Math.min(size.w / (maxX - minX), size.h / (maxY - minY), 1.15)
    return {
      scale,
      x: size.w / 2 - ((minX + maxX) / 2) * scale,
      y: size.h / 2 - ((minY + maxY) / 2) * scale,
    }
  }, [located, size])

  const [view, setView] = useState(fit)
  useEffect(() => setView(fit), [fit])

  const onWheel = (e: KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault()
    const stage = e.target.getStage()
    if (!stage) return
    const pointer = stage.getPointerPosition()
    if (!pointer) return
    const old = view.scale
    const next = Math.min(2.6, Math.max(0.35, old * (e.evt.deltaY > 0 ? 0.9 : 1.1)))
    // 以指针为锚缩放
    setView({
      scale: next,
      x: pointer.x - ((pointer.x - view.x) / old) * next,
      y: pointer.y - ((pointer.y - view.y) / old) * next,
    })
  }

  const byId = useMemo(() => new Map(located.map((l) => [l.id, l])), [located])

  // 通路（麻绳虚线）：两端都在图上才画，成对去重。
  const edges = useMemo(() => {
    const seen = new Set<string>()
    const out: { a: SandboxLocation; b: SandboxLocation }[] = []
    for (const l of located) {
      for (const cid of l.connections || []) {
        const other = byId.get(cid)
        if (!other) continue
        const key = [l.id, other.id].sort().join('|')
        if (seen.has(key)) continue
        seen.add(key)
        out.push({ a: l, b: other })
      }
    }
    return out
  }, [located, byId])

  // 地貌晕染：每个场景格的六邻空格铺一层淡地貌色（大陆感）；被场景占用的格不铺。
  const halos = useMemo(() => {
    const occupied = new Set(located.map((l) => `${l.map!.q},${l.map!.r}`))
    const cells = new Map<string, string>()
    for (const l of located) {
      for (const [dq, dr] of AXIAL_DIRS) {
        const key = `${l.map!.q + dq},${l.map!.r + dr}`
        if (!occupied.has(key) && !cells.has(key)) cells.set(key, l.map!.biome)
      }
    }
    return Array.from(cells, ([key, biome]) => {
      const [q, r] = key.split(',').map(Number)
      return { q, r, biome }
    })
  }, [located])

  return (
    <div ref={wrapRef} style={{ height, borderRadius: 8, overflow: 'hidden', position: 'relative', border: '1px solid var(--color-border)' }}>
      <Stage width={size.w} height={size.h} onWheel={onWheel} draggable
        x={view.x} y={view.y} scaleX={view.scale} scaleY={view.scale}
        onDragEnd={(e: KonvaEventObject<DragEvent>) => {
          const s = e.target as Konva.Stage
          if (s === e.target.getStage()) setView((v) => ({ ...v, x: s.x(), y: s.y() }))
        }}
        style={{ background: 'radial-gradient(ellipse 75% 65% at 50% 42%, #221c12 0%, #151109 68%, #0c0a06 100%)', cursor: 'grab' }}>
        <Layer>
          {/* 地貌晕染层 */}
          {halos.map((h) => {
            const p = hexXY(h.q, h.r)
            return (
              <RegularPolygon key={`halo-${h.q},${h.r}`} x={p.x} y={p.y} sides={6} radius={R - 1}
                fill={biomeOf(h.biome).fill} opacity={0.18} listening={false} />
            )
          })}
          {/* 通路 */}
          {edges.map(({ a, b }) => {
            const pa = hexXY(a.map!.q, a.map!.r)
            const pb = hexXY(b.map!.q, b.map!.r)
            return (
              <Line key={`e-${a.id}-${b.id}`} points={[pa.x, pa.y, pb.x, pb.y]} stroke={TWINE}
                strokeWidth={1.6} dash={[7, 5]} listening={false} />
            )
          })}
          {/* 场景瓦片 */}
          {located.map((l) => {
            const p = hexXY(l.map!.q, l.map!.r)
            const style = biomeOf(l.map!.biome)
            const unknown = l.known === false            // 仅 KP 上帝视角会收到
            const heard = !unknown && !l.visited && !l.current   // 听说过未到访
            const clickable = !!onPick && !l.current && !disabled && !unknown
            return (
              <Group key={l.id} x={p.x} y={p.y}
                draggable={!!editable}
                onDragEnd={(e: KonvaEventObject<DragEvent>) => {
                  const node = e.target
                  const { q, r } = xyToHex(node.x(), node.y())
                  node.position(p)              // 复位；父组件更新 map 后由 props 重绘到新格
                  onMoveScene?.(l.id, q, r)
                }}
                onClick={() => clickable && onPick?.(l)}
                onTap={() => clickable && onPick?.(l)}
                onMouseEnter={(e) => { const st = e.target.getStage(); if (st) st.container().style.cursor = editable ? 'move' : clickable ? 'pointer' : 'grab' }}
                onMouseLeave={(e) => { const st = e.target.getStage(); if (st) st.container().style.cursor = 'grab' }}>
                {l.current && (
                  <Circle radius={R * 1.75} listening={false}
                    fillRadialGradientStartRadius={0} fillRadialGradientEndRadius={R * 1.75}
                    fillRadialGradientColorStops={[0, 'rgba(212,162,78,0.30)', 1, 'rgba(212,162,78,0)']} />
                )}
                <RegularPolygon sides={6} radius={R - 2} fill={style.fill}
                  stroke={l.current ? CANDLE : l.visited ? 'rgba(212,162,78,0.35)' : 'rgba(138,122,92,0.6)'}
                  strokeWidth={l.current ? 2.4 : 1.2}
                  dash={heard || unknown ? [6, 4] : undefined}
                  shadowColor={l.current ? CANDLE : undefined}
                  shadowBlur={l.current ? 14 : 0} shadowOpacity={0.5} />
                <HexDeco q={l.map!.q} r={l.map!.r} biome={l.map!.biome} />
                {/* 危险度内环（calm 不标）：模组页/带 danger 的 payload 才有 */}
                {!unknown && l.danger && DANGER_COLORS[l.danger] && (
                  <RegularPolygon sides={6} radius={R - 8} stroke={DANGER_COLORS[l.danger]}
                    strokeWidth={1.4} opacity={0.75} listening={false} />
                )}
                {/* 迷雾覆层：听说过=蒙尘；未知=浓雾（仅上帝视角出现） */}
                {(heard || unknown) && (
                  <RegularPolygon sides={6} radius={R - 2} fill={FOG}
                    opacity={unknown ? 0.72 : 0.42} listening={false} />
                )}
                {unknown && (
                  <Text text="未探明" x={-R} y={-6} width={R * 2} align="center" fontSize={11}
                    fill="rgba(190,175,140,0.55)" listening={false} />
                )}
                {/* 在场队友徽章（首字） */}
                {(l.party || []).slice(0, 4).map((name, i) => (
                  <Group key={name + i} x={R * 0.62 - i * 15} y={-R * 0.78} listening={false}>
                    <Circle radius={8} fill="#2a2320" stroke={CANDLE} strokeWidth={1} />
                    <Text text={name.slice(0, 1)} x={-8} y={-5.5} width={16} align="center"
                      fontSize={10} fill={PARCH} />
                  </Group>
                ))}
                {/* 已发现线索计数 */}
                {(l.clues?.length || 0) > 0 && !unknown && (
                  <Group x={-R * 0.66} y={-R * 0.78} listening={false}>
                    <Circle radius={8} fill={THREAD} opacity={0.92} />
                    <Text text={String(l.clues!.length)} x={-8} y={-5.5} width={16} align="center"
                      fontSize={10} fontStyle="bold" fill={PARCH} />
                  </Group>
                )}
              </Group>
            )
          })}
          {/* 名牌层：所有瓦片之后统一绘制，避免被相邻瓦片盖住一角 */}
          {located.map((l) => {
            const p = hexXY(l.map!.q, l.map!.r)
            const unknown = l.known === false
            const nameW = Math.max(44, l.name.length * 13 + 14)
            return (
              <Group key={`label-${l.id}`} x={p.x} y={p.y} listening={false}>
                <Rect x={-nameW / 2} y={R - 6} width={nameW} height={20} cornerRadius={3}
                  fill={unknown ? 'rgba(20,16,10,0.82)' : 'rgba(26,21,13,0.88)'}
                  stroke={l.current ? CANDLE : 'rgba(138,122,92,0.5)'} strokeWidth={l.current ? 1.2 : 0.8} />
                <Text text={l.name} x={-nameW / 2} y={R - 1} width={nameW} align="center"
                  fontSize={12.5} fontStyle={l.current ? 'bold' : 'normal'}
                  fill={unknown ? 'rgba(190,175,140,0.5)' : l.current ? CANDLE : PARCH} />
              </Group>
            )
          })}
        </Layer>
      </Stage>
      {!located.length && (
        <div className="absolute inset-0 flex items-center justify-center text-xs"
          style={{ color: 'var(--color-text-secondary)', pointerEvents: 'none' }}>
          尚无可落格的已知地点——探索或提及地名后，沙盘会亮起相应区域
        </div>
      )}
    </div>
  )
}
