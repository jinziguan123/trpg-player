import { useMemo, useState, useRef, useCallback, useLayoutEffect } from 'react'
import dagre from 'dagre'
import { GiPadlock } from 'react-icons/gi'

interface Scene { id: string; name?: string; title?: string; connections?: string[] }
interface NPC { id: string; name?: string; initial_location?: string }
interface Clue { id: string; name?: string; location?: string }

interface Pt { x: number; y: number }
interface GNode { id: string; x: number; y: number; w: number; h: number; name: string; npcs: string[]; clues: string[]; orphan?: boolean }
interface GEdge { id: string; points: Pt[] }

/** 用 Catmull-Rom 把折线路点平滑成曲线（穿过 dagre 给的每个路点）。 */
function smoothPath(pts: Pt[]): string {
  if (pts.length < 2) return ''
  let d = `M${pts[0].x},${pts[0].y}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i]
    const p1 = pts[i]
    const p2 = pts[i + 1]
    const p3 = pts[i + 2] || p2
    const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6
    const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2.x},${p2.y}`
  }
  return d
}

const NODE_W = 210
const ACCENT = '#8b2500'
const sceneName = (s: Scene) => s.name || s.title || s.id || '(未命名)'
const nodeHeight = (npcs: string[], clues: string[]) => 34 + (npcs.length ? 22 : 0) + (clues.length ? 22 : 0)

/** 用 dagre 自顶向下分层布局，返回带绝对坐标的节点与边（自绘 SVG，不依赖 react-flow 渲染）。 */
function layout(scenes: Scene[], npcs: NPC[], clues: Clue[]) {
  const sceneIds = new Set(scenes.map((s) => s.id))
  const raw = scenes.map((s) => ({
    id: s.id, name: sceneName(s),
    npcs: npcs.filter((n) => n.initial_location === s.id).map((n) => n.name || '?'),
    clues: clues.filter((c) => c.location === s.id).map((c) => c.name || '?'),
  }))
  const orphanNpcs = npcs.filter((n) => !n.initial_location || !sceneIds.has(n.initial_location)).map((n) => n.name || '?')
  const orphanClues = clues.filter((c) => !c.location || !sceneIds.has(c.location)).map((c) => c.name || '?')
  if (orphanNpcs.length || orphanClues.length) {
    raw.push({ id: '__orphan__', name: '未归类', npcs: orphanNpcs, clues: orphanClues })
  }

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
  // edgesep 拉开平行边、ranksep 拉大层距，配合 dagre 的折线路由避免连线/箭头重叠。
  g.setGraph({ rankdir: 'TB', nodesep: 55, ranksep: 90, edgesep: 30, marginx: 24, marginy: 24 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of raw) g.setNode(n.id, { width: NODE_W, height: nodeHeight(n.npcs, n.clues) })
  for (const [a, b] of conn) g.setEdge(a, b)
  dagre.layout(g)

  const nodes: GNode[] = raw.map((n) => {
    const p = g.node(n.id)
    return { ...n, x: p.x, y: p.y, w: p.width as number, h: p.height as number, orphan: n.id === '__orphan__' }
  })
  // 用 dagre 计算出的边路点（已为平行边/回边做了避让），而非节点中心直连。
  const edges: GEdge[] = conn.flatMap(([a, b]) => {
    const e = g.edge(a, b) as { points?: Pt[] } | undefined
    const points = e?.points && e.points.length >= 2 ? e.points : []
    return points.length ? [{ id: `${a}->${b}`, points }] : []
  })
  const gr = g.graph()
  return { nodes, edges, width: (gr.width as number) || 400, height: (gr.height as number) || 300 }
}

export function ModuleGraph({ scenes, npcs, clues }: { scenes: Scene[]; npcs: NPC[]; clues: Clue[] }) {
  const { nodes, edges, width, height } = useMemo(() => layout(scenes, npcs, clues), [scenes, npcs, clues])
  const [zoom, setZoom] = useState(1)
  const wrapRef = useRef<HTMLDivElement>(null)
  const fittedRef = useRef(false)

  // 挂载时按容器大小自动适配缩放，避免小图缩在左上角、留大片空白。
  useLayoutEffect(() => {
    if (fittedRef.current || !wrapRef.current) return
    const cw = wrapRef.current.clientWidth - 24
    const ch = wrapRef.current.clientHeight - 24
    if (cw > 0 && ch > 0) {
      setZoom(Math.max(0.4, Math.min(cw / width, ch / height, 1.3)))
      fittedRef.current = true
    }
  }, [width, height])

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return
    e.preventDefault()
    setZoom((z) => Math.min(1.6, Math.max(0.4, z - e.deltaY * 0.001)))
  }, [])

  if (scenes.length === 0) {
    return <p className="text-sm text-center py-8" style={{ color: 'var(--color-text-secondary)' }}>暂无场景，无法生成关系图</p>
  }

  return (
    <div className="relative" style={{ height: '70vh', border: '1px solid var(--color-border)', borderRadius: 6, overflow: 'auto', background: 'var(--color-bg-tertiary)', display: 'flex', alignItems: 'safe center', justifyContent: 'safe center' }} ref={wrapRef} onWheel={onWheel}>
      <div className="absolute top-2 right-2 z-10 flex gap-1">
        <button onClick={() => setZoom((z) => Math.max(0.4, z - 0.15))} className="btn-secondary !px-2 !py-0.5 text-sm">－</button>
        <button onClick={() => setZoom(1)} className="btn-secondary !px-2 !py-0.5 text-xs">100%</button>
        <button onClick={() => setZoom((z) => Math.min(1.6, z + 0.15))} className="btn-secondary !px-2 !py-0.5 text-sm">＋</button>
      </div>
      <div style={{ width: width * zoom, height: height * zoom, position: 'relative', flexShrink: 0, margin: 12 }}>
        <div style={{ width, height, position: 'absolute', top: 0, left: 0, transformOrigin: '0 0', transform: `scale(${zoom})` }}>
          <svg width={width} height={height} style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}>
            <defs>
              <marker id="mg-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
                <path d="M0,0 L7,3 L0,6 Z" fill={ACCENT} />
              </marker>
            </defs>
            {edges.map((e) => (
              <path key={e.id} d={smoothPath(e.points)}
                fill="none" stroke={ACCENT} strokeWidth={1.5} markerEnd="url(#mg-arrow)" opacity={0.85} />
            ))}
          </svg>
          {nodes.map((n) => (
            <div key={n.id} className="rounded-md text-xs shadow-sm absolute"
              style={{
                left: n.x - n.w / 2, top: n.y - n.h / 2, width: n.w,
                background: n.orphan ? 'var(--color-bg-tertiary)' : 'var(--color-bg-card)',
                border: `1px solid ${n.orphan ? 'var(--color-border)' : ACCENT}`,
              }}>
              <div className="px-2 py-1 font-semibold rounded-t-md truncate"
                style={{ background: n.orphan ? 'transparent' : 'rgba(139,37,0,0.08)', color: 'var(--color-text-accent)' }}>
                {n.name}
              </div>
              <div className="px-2 py-1 space-y-0.5">
                {n.npcs.length > 0 && <div><span style={{ color: 'var(--color-text-secondary)' }}>NPC：</span>{n.npcs.join('、')}</div>}
                {n.clues.length > 0 && <div style={{ color: 'var(--color-danger)' }} className="flex items-start gap-1"><GiPadlock className="mt-0.5 flex-shrink-0" /><span><span style={{ opacity: 0.7 }}>线索：</span>{n.clues.join('、')}</span></div>}
                {n.npcs.length === 0 && n.clues.length === 0 && <div style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>（空场景）</div>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
