import { useMemo, useState } from 'react'
import {
  ReactFlow, ReactFlowProvider, Background, Controls,
  Handle, Position, BaseEdge, MarkerType, useInternalNode,
  type Node, type Edge, type EdgeProps, type InternalNode,
} from '@xyflow/react'
import dagre from 'dagre'
import { GiPadlock } from 'react-icons/gi'
import { X } from 'lucide-react'
import '@xyflow/react/dist/base.css'

interface Scene { id: string; name?: string; title?: string; description?: string; danger?: string; atmosphere?: string; connections?: string[] }
interface NPC { id: string; name?: string; description?: string; personality?: string; secrets?: string[]; initial_location?: string; skills?: Record<string, number> }
interface Clue { id: string; name?: string; description?: string; location?: string; trigger_condition?: string }
interface SceneData extends Record<string, unknown> { name: string; npcs: string[]; clues: string[]; orphan?: boolean }

const NODE_W = 210
const ACCENT = '#d4a24e'
const DANGER_META: Record<string, { label: string; color: string }> = {
  calm: { label: '平静', color: 'var(--color-text-secondary)' },
  uneasy: { label: '不安', color: '#cfa93f' },
  dangerous: { label: '危险', color: '#d1703c' },
  deadly: { label: '致命', color: 'var(--color-danger)' },
}
const sceneName = (s: Scene) => s.name || s.title || s.id || '(未命名)'
const nodeHeight = (d: SceneData) => 40 + (d.npcs.length ? 22 : 0) + (d.clues.length ? 22 : 0)

/** 自定义场景节点：标题 + 该场景的 NPC / 线索（线索按剧透红，挂锁矢量图标）。点击查看详情。 */
function SceneNode({ data, selected }: { data: SceneData; selected?: boolean }) {
  const hStyle = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0 }
  const borderColor = data.orphan ? 'var(--color-border)' : ACCENT
  return (
    <div
      className="rounded-md text-xs cursor-pointer transition-shadow"
      style={{
        width: NODE_W,
        background: data.orphan ? 'var(--color-bg-tertiary)' : 'var(--color-bg-card)',
        border: `${selected ? 2 : 1}px solid ${borderColor}`,
        boxShadow: selected ? `0 0 0 3px rgba(212,162,78,0.3)` : '0 1px 2px rgba(0,0,0,0.1)',
      }}
    >
      <Handle type="target" position={Position.Top} style={hStyle} />
      <Handle type="source" position={Position.Bottom} style={hStyle} />
      <div className="px-2 py-1 font-semibold rounded-t-md truncate" style={{ background: data.orphan ? 'transparent' : 'rgba(212,162,78,0.1)', color: 'var(--color-text-accent)' }}>{data.name}</div>
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

const ORPHAN_ID = '__orphan__'

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
  if (orphanNpcs.length || orphanClues.length) raw.push({ id: ORPHAN_ID, data: { name: '未归类', npcs: orphanNpcs, clues: orphanClues, orphan: true } })

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

/** 点击节点弹出的详情面板：场景描述、连接、NPC（含秘密/技能）、线索（含发现条件）。 */
function DetailPanel({ scene, npcs, clues, sceneNameById, orphan, orphanNpcs, orphanClues, onClose }: {
  scene?: Scene
  npcs: NPC[]
  clues: Clue[]
  sceneNameById: Map<string, string>
  orphan?: boolean
  orphanNpcs: NPC[]
  orphanClues: Clue[]
  onClose: () => void
}) {
  const title = orphan ? '未归类' : scene ? sceneName(scene) : ''
  const shownNpcs = orphan ? orphanNpcs : npcs
  const shownClues = orphan ? orphanClues : clues
  const connNames = (scene?.connections || [])
    .map((id) => sceneNameById.get(id))
    .filter((x): x is string => Boolean(x))

  return (
    <div
      className="absolute top-0 right-0 h-full overflow-y-auto p-3 text-sm"
      style={{ width: 320, background: 'var(--color-bg-card)', borderLeft: '1px solid var(--color-border)', zIndex: 5, boxShadow: '-4px 0 12px rgba(0,0,0,0.08)' }}
    >
      <div className="flex items-center justify-between mb-2 sticky top-0" style={{ background: 'var(--color-bg-card)' }}>
        <h3 className="font-semibold text-base truncate" style={{ color: 'var(--color-text-accent)' }}>{title}</h3>
        <button onClick={onClose} className="p-1 rounded hover:bg-[var(--color-bg-tertiary)] flex-shrink-0" title="关闭"><X size={16} /></button>
      </div>

      {!orphan && (
        <PanelBlock label="危险度 / 氛围">
          <div className="flex items-center gap-2">
            {(() => { const m = DANGER_META[scene?.danger || 'calm'] || DANGER_META.calm; return <span className="badge" style={{ color: m.color, borderColor: m.color }}>{m.label}</span> })()}
            <span style={{ color: 'var(--color-text-secondary)' }}>{scene?.atmosphere || '—'}</span>
          </div>
        </PanelBlock>
      )}
      {!orphan && (
        <PanelBlock label="描述">
          <p className="whitespace-pre-wrap" style={{ color: 'var(--color-text-primary)' }}>{scene?.description || '—'}</p>
        </PanelBlock>
      )}
      {!orphan && (
        <PanelBlock label="连接场景">
          <p style={{ color: 'var(--color-text-secondary)' }}>{connNames.length ? connNames.join('、') : '—'}</p>
        </PanelBlock>
      )}

      <PanelBlock label={`NPC（${shownNpcs.length}）`}>
        {shownNpcs.length === 0 && <p style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>—</p>}
        {shownNpcs.map((n) => (
          <div key={n.id} className="rounded-md p-2 mb-2" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
            <div className="font-semibold">{n.name || '(未命名)'}</div>
            {n.description && <p className="whitespace-pre-wrap mt-0.5" style={{ color: 'var(--color-text-primary)' }}>{n.description}</p>}
            {n.personality && <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-secondary)' }}>性格：{n.personality}</p>}
            {n.secrets && n.secrets.filter((s) => s.trim()).length > 0 && (
              <div className="mt-1 flex items-start gap-1 text-xs" style={{ color: 'var(--color-danger)' }}>
                <GiPadlock className="mt-0.5 flex-shrink-0" />
                <span className="whitespace-pre-wrap">{n.secrets.filter((s) => s.trim()).join('\n')}</span>
              </div>
            )}
            {n.skills && Object.keys(n.skills).length > 0 && (
              <p className="mt-1 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                技能：{Object.entries(n.skills).map(([k, v]) => `${k} ${v}`).join('、')}
              </p>
            )}
          </div>
        ))}
      </PanelBlock>

      <PanelBlock label={`线索（${shownClues.length}）`}>
        {shownClues.length === 0 && <p style={{ color: 'var(--color-text-secondary)', opacity: 0.6 }}>—</p>}
        {shownClues.map((c) => (
          <div key={c.id} className="rounded-md p-2 mb-2" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
            <div className="font-semibold flex items-center gap-1" style={{ color: 'var(--color-danger)' }}><GiPadlock className="flex-shrink-0" />{c.name || '(未命名)'}</div>
            {c.description && <p className="whitespace-pre-wrap mt-0.5" style={{ color: 'var(--color-danger)' }}>{c.description}</p>}
            {c.trigger_condition && <p className="mt-0.5 text-xs" style={{ color: 'var(--color-text-secondary)' }}>发现条件：{c.trigger_condition}</p>}
          </div>
        ))}
      </PanelBlock>
    </div>
  )
}

function PanelBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="text-xs font-semibold mb-1" style={{ color: 'var(--color-text-secondary)' }}>{label}</div>
      {children}
    </div>
  )
}

export function ModuleGraph({ scenes, npcs, clues }: { scenes: Scene[]; npcs: NPC[]; clues: Clue[] }) {
  const { nodes, edges } = useMemo(() => build(scenes, npcs, clues), [scenes, npcs, clues])
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const sceneNameById = useMemo(() => new Map(scenes.map((s) => [s.id, sceneName(s)])), [scenes])
  const sceneIds = useMemo(() => new Set(scenes.map((s) => s.id)), [scenes])
  const orphanNpcs = useMemo(() => npcs.filter((n) => !n.initial_location || !sceneIds.has(n.initial_location)), [npcs, sceneIds])
  const orphanClues = useMemo(() => clues.filter((c) => !c.location || !sceneIds.has(c.location)), [clues, sceneIds])

  if (scenes.length === 0) {
    return <p className="text-sm text-center py-8" style={{ color: 'var(--color-text-secondary)' }}>暂无场景，无法生成关系图</p>
  }

  const selectedScene = selectedId && selectedId !== ORPHAN_ID ? scenes.find((s) => s.id === selectedId) : undefined
  const isOrphan = selectedId === ORPHAN_ID
  const panelNpcs = selectedScene ? npcs.filter((n) => n.initial_location === selectedScene.id) : []
  const panelClues = selectedScene ? clues.filter((c) => c.location === selectedScene.id) : []

  return (
    <div style={{ height: 'calc(100vh - 230px)', minHeight: 460, position: 'relative', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-bg-tertiary)', overflow: 'hidden' }}>
      <ReactFlowProvider>
        <ReactFlow
          defaultNodes={nodes}
          defaultEdges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          defaultEdgeOptions={defaultEdgeOptions}
          nodesConnectable={false}
          onNodeClick={(_, node) => setSelectedId(node.id)}
          onPaneClick={() => setSelectedId(null)}
          fitView
          minZoom={0.3}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="var(--color-border)" gap={18} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </ReactFlowProvider>
      {selectedId && (selectedScene || isOrphan) && (
        <DetailPanel
          scene={selectedScene}
          npcs={panelNpcs}
          clues={panelClues}
          sceneNameById={sceneNameById}
          orphan={isOrphan}
          orphanNpcs={orphanNpcs}
          orphanClues={orphanClues}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  )
}
