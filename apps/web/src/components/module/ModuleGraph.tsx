import { useMemo, useState, useRef, useCallback, useLayoutEffect } from 'react'
import dagre from 'dagre'
import { GiPadlock } from 'react-icons/gi'

interface Scene { id: string; name?: string; title?: string; connections?: string[] }
interface NPC { id: string; name?: string; initial_location?: string }
interface Clue { id: string; name?: string; location?: string }

interface Pt { x: number; y: number }
interface GNode { id: string; x: number; y: number; w: number; h: number; name: string; npcs: string[]; clues: string[]; orphan?: boolean }
interface GEdge { id: string; source: string; target: string; points: Pt[] }
interface Rect { left: number; right: number; top: number; bottom: number }

const NODE_W = 210
const ACCENT = '#8b2500'
const sceneName = (s: Scene) => s.name || s.title || s.id || '(未命名)'
const nodeHeight = (npcs: string[], clues: string[]) => 34 + (npcs.length ? 22 : 0) + (clues.length ? 22 : 0)

const inside = (p: Pt, r: Rect) => p.x >= r.left && p.x <= r.right && p.y >= r.top && p.y <= r.bottom

/** O 在矩形外、I 在矩形内：求线段与矩形边界的交点（Liang-Barsky 入口 t）。 */
function clipSeg(O: Pt, I: Pt, r: Rect): Pt {
  const dx = I.x - O.x, dy = I.y - O.y
  const p = [-dx, dx, -dy, dy]
  const q = [O.x - r.left, r.right - O.x, O.y - r.top, r.bottom - O.y]
  let t0 = 0
  for (let i = 0; i < 4; i++) {
    if (p[i] === 0) { if (q[i] < 0) return O } else { const t = q[i] / p[i]; if (p[i] < 0) t0 = Math.max(t0, t) }
  }
  return { x: O.x + t0 * dx, y: O.y + t0 * dy }
}

/** 把路点两端裁到节点边框上（消除「箭头穿过边框指向中心」）。 */
function clipPath(points: Pt[], src: Rect, tgt: Rect): Pt[] {
  let pts = points
  // 起点：去掉落在 source 框内的点，补上边界交点
  let i = 0
  while (i < pts.length - 1 && inside(pts[i], src)) i++
  if (i > 0) pts = [clipSeg(pts[i], pts[i - 1], src), ...pts.slice(i)]
  // 终点：去掉落在 target 框内的点，补上边界交点（箭头落在此处）
  let j = pts.length - 1
  while (j > 0 && inside(pts[j], tgt)) j--
  if (j < pts.length - 1) pts = [...pts.slice(0, j + 1), clipSeg(pts[j], pts[j + 1], tgt)]
  return pts
}

/** Catmull-Rom 平滑成曲线穿过路点。 */
function smoothPath(pts: Pt[]): string {
  if (pts.length < 2) return ''
  let d = `M${pts[0].x},${pts[0].y}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2
    const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6
    const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2.x},${p2.y}`
  }
  return d
}

function layout(scenes: Scene[], npcs: NPC[], clues: Clue[]) {
  const sceneIds = new Set(scenes.map((s) => s.id))
  const raw = scenes.map((s) => ({
    id: s.id, name: sceneName(s),
    npcs: npcs.filter((n) => n.initial_location === s.id).map((n) => n.name || '?'),
    clues: clues.filter((c) => c.location === s.id).map((c) => c.name || '?'),
  }))
  const orphanNpcs = npcs.filter((n) => !n.initial_location || !sceneIds.has(n.initial_location)).map((n) => n.name || '?')
  const orphanClues = clues.filter((c) => !c.location || !sceneIds.has(c.location)).map((c) => c.name || '?')
  if (orphanNpcs.length || orphanClues.length) raw.push({ id: '__orphan__', name: '未归类', npcs: orphanNpcs, clues: orphanClues })

  const conn: [string, string][] = []
  const seen = new Set<string>()
  for (const s of scenes) {
    for (const t of s.connections || []) {
      if (!sceneIds.has(t)) continue
      const k = `${s.id}->${t}`
      if (seen.has(k)) continue
      seen.add(k)
      conn.push([s.id, t])
    }
  }

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 55, ranksep: 90, edgesep: 30, marginx: 24, marginy: 24 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of raw) g.setNode(n.id, { width: NODE_W, height: nodeHeight(n.npcs, n.clues) })
  for (const [a, b] of conn) g.setEdge(a, b)
  dagre.layout(g)

  const nodes: GNode[] = raw.map((n) => {
    const p = g.node(n.id)
    return { ...n, x: p.x, y: p.y, w: p.width as number, h: p.height as number, orphan: n.id === '__orphan__' }
  })
  const edges: GEdge[] = conn.flatMap(([a, b]) => {
    const e = g.edge(a, b) as { points?: Pt[] } | undefined
    const points = e?.points && e.points.length >= 2 ? e.points : []
    return points.length ? [{ id: `${a}->${b}`, source: a, target: b, points }] : []
  })
  const gr = g.graph()
  return { nodes, edges, width: (gr.width as number) || 400, height: (gr.height as number) || 300 }
}

const PAD = 5 // 裁剪时把节点框外扩几像素，箭头落在框外一点更清爽

export function ModuleGraph({ scenes, npcs, clues }: { scenes: Scene[]; npcs: NPC[]; clues: Clue[] }) {
  const { nodes, edges, width, height } = useMemo(() => layout(scenes, npcs, clues), [scenes, npcs, clues])
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [moved, setMoved] = useState<Record<string, Pt>>({})
  const wrapRef = useRef<HTMLDivElement>(null)
  const fittedRef = useRef(false)
  const drag = useRef<{ mode: 'pan' | string | null; sx: number; sy: number; ox: number; oy: number }>({ mode: null, sx: 0, sy: 0, ox: 0, oy: 0 })

  const fitView = useCallback(() => {
    if (!wrapRef.current) return
    const cw = wrapRef.current.clientWidth, ch = wrapRef.current.clientHeight
    if (cw <= 0 || ch <= 0) return
    const z = Math.max(0.4, Math.min((cw - 32) / width, (ch - 32) / height, 1.3))
    setZoom(z)
    setPan({ x: (cw - width * z) / 2, y: (ch - height * z) / 2 })
  }, [width, height])

  useLayoutEffect(() => {
    if (fittedRef.current) return
    fitView()
    fittedRef.current = true
  }, [fitView])

  const center = useCallback((id: string): Pt => {
    const m = moved[id]
    if (m) return m
    const n = nodes.find((nn) => nn.id === id)!
    return { x: n.x, y: n.y }
  }, [moved, nodes])

  const onPointerDown = (e: React.PointerEvent) => {
    const nodeId = (e.target as HTMLElement).closest('[data-node]')?.getAttribute('data-node')
    if (nodeId) {
      const c = center(nodeId)
      drag.current = { mode: nodeId, sx: e.clientX, sy: e.clientY, ox: c.x, oy: c.y }
    } else {
      drag.current = { mode: 'pan', sx: e.clientX, sy: e.clientY, ox: pan.x, oy: pan.y }
    }
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 合成事件/无活动指针时忽略 */ }
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d.mode) return
    if (d.mode === 'pan') {
      setPan({ x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) })
    } else {
      setMoved((m) => ({ ...m, [d.mode as string]: { x: d.ox + (e.clientX - d.sx) / zoom, y: d.oy + (e.clientY - d.sy) / zoom } }))
    }
  }
  const onPointerUp = () => { drag.current.mode = null }

  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    const rect = wrapRef.current!.getBoundingClientRect()
    const mx = e.clientX - rect.left, my = e.clientY - rect.top
    setZoom((z) => {
      const nz = Math.max(0.3, Math.min(2, z * (e.deltaY < 0 ? 1.1 : 0.9)))
      setPan((p) => ({ x: mx - (mx - p.x) / z * nz, y: my - (my - p.y) / z * nz }))
      return nz
    })
  }, [])

  if (scenes.length === 0) {
    return <p className="text-sm text-center py-8" style={{ color: 'var(--color-text-secondary)' }}>暂无场景，无法生成关系图</p>
  }

  const rectOf = (id: string): Rect => {
    const n = nodes.find((nn) => nn.id === id)!
    const c = center(id)
    return { left: c.x - n.w / 2 - PAD, right: c.x + n.w / 2 + PAD, top: c.y - n.h / 2 - PAD, bottom: c.y + n.h / 2 + PAD }
  }

  return (
    <div className="relative" ref={wrapRef} onWheel={onWheel}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerLeave={onPointerUp}
      style={{ height: '70vh', border: '1px solid var(--color-border)', borderRadius: 6, overflow: 'hidden', background: 'var(--color-bg-tertiary)', cursor: drag.current.mode === 'pan' ? 'grabbing' : 'grab', touchAction: 'none' }}>
      <div className="absolute top-2 right-2 z-10 flex gap-1">
        <button onClick={() => setZoom((z) => Math.max(0.3, z - 0.15))} className="btn-secondary !px-2 !py-0.5 text-sm">－</button>
        <button onClick={() => { setMoved({}); fitView() }} className="btn-secondary !px-2 !py-0.5 text-xs">重置</button>
        <button onClick={() => setZoom((z) => Math.min(2, z + 0.15))} className="btn-secondary !px-2 !py-0.5 text-sm">＋</button>
      </div>
      <div style={{ width, height, position: 'absolute', top: 0, left: 0, transformOrigin: '0 0', transform: `translate(${pan.x}px,${pan.y}px) scale(${zoom})` }}>
        <svg width={width} height={height} style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none', overflow: 'visible' }}>
          <defs>
            <marker id="mg-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L7,3 L0,6 Z" fill={ACCENT} />
            </marker>
          </defs>
          {edges.map((e) => {
            const sR = rectOf(e.source), tR = rectOf(e.target)
            // 端点被拖动过的边，按当前中心直连；否则用 dagre 路由路点。最后统一裁到边框。
            const base = (moved[e.source] || moved[e.target]) ? [center(e.source), center(e.target)] : e.points
            const clipped = clipPath(base, sR, tR)
            return <path key={e.id} d={smoothPath(clipped)} fill="none" stroke={ACCENT} strokeWidth={1.5} markerEnd="url(#mg-arrow)" opacity={0.85} />
          })}
        </svg>
        {nodes.map((n) => {
          const c = center(n.id)
          return (
            <div key={n.id} data-node={n.id}
              className="rounded-md text-xs shadow-sm absolute select-none"
              style={{
                left: c.x - n.w / 2, top: c.y - n.h / 2, width: n.w, cursor: 'move',
                background: n.orphan ? 'var(--color-bg-tertiary)' : 'var(--color-bg-card)',
                border: `1px solid ${n.orphan ? 'var(--color-border)' : ACCENT}`,
              }}>
              <div className="px-2 py-1 font-semibold rounded-t-md truncate" style={{ background: n.orphan ? 'transparent' : 'rgba(139,37,0,0.08)', color: 'var(--color-text-accent)' }}>{n.name}</div>
              <div className="px-2 py-1 space-y-0.5">
                {n.npcs.length > 0 && <div><span style={{ color: 'var(--color-text-secondary)' }}>NPC：</span>{n.npcs.join('、')}</div>}
                {n.clues.length > 0 && <div style={{ color: 'var(--color-danger)' }} className="flex items-start gap-1"><GiPadlock className="mt-0.5 flex-shrink-0" /><span><span style={{ opacity: 0.7 }}>线索：</span>{n.clues.join('、')}</span></div>}
                {n.npcs.length === 0 && n.clues.length === 0 && <div style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>（空场景）</div>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
