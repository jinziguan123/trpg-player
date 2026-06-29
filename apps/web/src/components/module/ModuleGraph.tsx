import { useMemo } from 'react'
import {
  ReactFlow, ReactFlowProvider, Background, Controls, MiniMap,
  Handle, Position, BaseEdge, MarkerType, useInternalNode,
  type Node, type Edge, type EdgeProps, type InternalNode,
} from '@xyflow/react'
import dagre from 'dagre'
import { GiPadlock } from 'react-icons/gi'
import '@xyflow/react/dist/base.css'

interface Scene { id: string; name?: string; title?: string; connections?: string[] }
interface NPC { id: string; name?: string; initial_location?: string }
interface Clue { id: string; name?: string; location?: string }
interface SceneData extends Record<string, unknown> { name: string; npcs: string[]; clues: string[]; orphan?: boolean }

const NODE_W = 210
const ACCENT = '#8b2500'
const sceneName = (s: Scene) => s.name || s.title || s.id || '(未命名)'
const nodeHeight = (d: SceneData) => 40 + (d.npcs.length ? 22 : 0) + (d.clues.length ? 22 : 0)

/** 自定义场景节点：标题 + 该场景的 NPC / 线索（线索按剧透红，挂锁矢量图标）。 */
function SceneNode({ data }: { data: SceneData }) {
  const hStyle = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0 }
  return (
    <div className="rounded-md text-xs shadow-sm" style={{ width: NODE_W, background: data.orphan ? 'var(--color-bg-tertiary)' : 'var(--color-bg-card)', border: `1px solid ${data.orphan ? 'var(--color-border)' : ACCENT}` }}>
      <Handle type="target" position={Position.Top} style={hStyle} />
      <Handle type="source" position={Position.Bottom} style={hStyle} />
      <div className="px-2 py-1 font-semibold rounded-t-md truncate" style={{ background: data.orphan ? 'transparent' : 'rgba(139,37,0,0.08)', color: 'var(--color-text-accent)' }}>{data.name}</div>
      <div className="px-2 py-1 space-y-0.5">
        {data.npcs.length > 0 && <div><span style={{ color: 'var(--color-text-secondary)' }}>NPC：</span>{data.npcs.join('、')}</div>}
        {data.clues.length > 0 && <div style={{ color: 'var(--color-danger)' }} className="flex items-start gap-1"><GiPadlock className="mt-0.5 flex-shrink-0" /><span><span style={{ opacity: 0.7 }}>线索：</span>{data.clues.join('、')}</span></div>}
        {data.npcs.length === 0 && data.clues.length === 0 && <div style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>（空场景）</div>}
      </div>
    </div>
  )
}

/** 求 node 边框上、朝向 other 中心方向的交点（react-flow 浮动边算法）。 */
function nodeIntersection(node: InternalNode, other: InternalNode) {
  const w = (node.measured.width ?? NODE_W) / 2, h = (node.measured.height ?? 60) / 2
  const x2 = node.internals.positionAbsolute.x + w, y2 = node.internals.positionAbsolute.y + h
  const x1 = other.internals.positionAbsolute.x + (other.measured.width ?? NODE_W) / 2
  const y1 = other.internals.positionAbsolute.y + (other.measured.height ?? 60) / 2
  const xx1 = (x1 - x2) / (2 * w) - (y1 - y2) / (2 * h)
  const yy1 = (x1 - x2) / (2 * w) + (y1 - y2) / (2 * h)
  const a = 1 / (Math.abs(xx1) + Math.abs(yy1) || 1)
  return { x: w * (a * xx1 + a * yy1) + x2, y: h * (-a * xx1 + a * yy1) + y2 }
}

/** 浮动边：连两节点朝向彼此的边框交点（箭头落在边框，不穿中心）；双向边左右错开避免重叠。 */
function FloatingEdge({ id, source, target, markerEnd, style }: EdgeProps) {
  const s = useInternalNode(source)
  const t = useInternalNode(target)
  if (!s || !t) return null
  const sp = nodeIntersection(s, t), tp = nodeIntersection(t, s)
  const mx = (sp.x + tp.x) / 2, my = (sp.y + tp.y) / 2
  const dx = tp.x - sp.x, dy = tp.y - sp.y, len = Math.hypot(dx, dy) || 1
  const off = (source < target ? 1 : -1) * 20  // 双向边两条朝相反方向弯，避免重叠
  const cx = mx + (-dy / len) * off, cy = my + (dx / len) * off
  return <BaseEdge id={id} path={`M${sp.x},${sp.y} Q${cx},${cy} ${tp.x},${tp.y}`} markerEnd={markerEnd} style={style} />
}

const nodeTypes = { scene: SceneNode }
const edgeTypes = { floating: FloatingEdge }
const defaultEdgeOptions = { type: 'floating', markerEnd: { type: MarkerType.ArrowClosed, color: ACCENT, width: 18, height: 18 }, style: { stroke: ACCENT, strokeWidth: 1.5 } }

function build(scenes: Scene[], npcs: NPC[], clues: Clue[]): { nodes: Node[]; edges: Edge[] } {
  const sceneIds = new Set(scenes.map((s) => s.id))
  const raw = scenes.map((s) => ({
    id: s.id,
    data: {
      name: sceneName(s),
      npcs: npcs.filter((n) => n.initial_location === s.id).map((n) => n.name || '?'),
      clues: clues.filter((c) => c.location === s.id).map((c) => c.name || '?'),
    } as SceneData,
  }))
  const orphanNpcs = npcs.filter((n) => !n.initial_location || !sceneIds.has(n.initial_location)).map((n) => n.name || '?')
  const orphanClues = clues.filter((c) => !c.location || !sceneIds.has(c.location)).map((c) => c.name || '?')
  if (orphanNpcs.length || orphanClues.length) raw.push({ id: '__orphan__', data: { name: '未归类', npcs: orphanNpcs, clues: orphanClues, orphan: true } })

  const edges: Edge[] = []
  const seen = new Set<string>()
  for (const s of scenes) {
    for (const tgt of s.connections || []) {
      if (!sceneIds.has(tgt)) continue
      const k = `${s.id}->${tgt}`
      if (seen.has(k)) continue
      seen.add(k)
      edges.push({ id: k, source: s.id, target: tgt, type: 'floating' })
    }
  }

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 55, ranksep: 90, marginx: 24, marginy: 24 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of raw) g.setNode(n.id, { width: NODE_W, height: nodeHeight(n.data) })
  for (const e of edges) g.setEdge(e.source, e.target)
  dagre.layout(g)

  const nodes: Node[] = raw.map((n) => {
    const p = g.node(n.id)
    return { id: n.id, type: 'scene', data: n.data, position: { x: p.x - NODE_W / 2, y: p.y - (p.height as number) / 2 }, width: NODE_W, height: p.height as number }
  })
  return { nodes, edges }
}

export function ModuleGraph({ scenes, npcs, clues }: { scenes: Scene[]; npcs: NPC[]; clues: Clue[] }) {
  const { nodes, edges } = useMemo(() => build(scenes, npcs, clues), [scenes, npcs, clues])

  if (scenes.length === 0) {
    return <p className="text-sm text-center py-8" style={{ color: 'var(--color-text-secondary)' }}>暂无场景，无法生成关系图</p>
  }

  return (
    <div style={{ height: '70vh', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-bg-tertiary)' }}>
      <ReactFlowProvider>
        <ReactFlow
          defaultNodes={nodes}
          defaultEdges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          defaultEdgeOptions={defaultEdgeOptions}
          nodesConnectable={false}
          fitView
          minZoom={0.3}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="var(--color-border)" gap={18} />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable nodeColor={ACCENT} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  )
}
