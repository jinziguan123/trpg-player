import { useMemo } from 'react'
import {
  ReactFlow, ReactFlowProvider, Handle, Position,
  type Node, type Edge, type NodeProps,
} from '@xyflow/react'
import dagre from 'dagre'
import { GiPositionMarker } from 'react-icons/gi'
import '@xyflow/react/dist/base.css'

/** 调查板（侦探桌风大地图）：已知地点是钉在暗色软木板上的卡片，麻绳虚线连出已知路径。
 *  点卡片 = 选中前往目标（外层走既有的二次确认 → travel 流）。 */
export interface BoardLocation {
  id: string
  name: string
  current: boolean
  visited: boolean
  connections?: string[]
  party?: string[]
}

const CARD_W = 148
const CANDLE = '#d4a24e'
const PARCH = '#e8dcc0'
const TWINE = '#8a7a5c'

/** 名字 → 稳定的轻微倾角（-2.4°~2.4°）：钉在板上的卡片不会摆得笔直。 */
function tiltOf(id: string): number {
  let h = 0
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) | 0
  return ((h % 100) / 100 - 0.5) * 4.8
}

interface CardData extends Record<string, unknown> {
  loc: BoardLocation
  disabled: boolean
  onPick: (loc: BoardLocation) => void
}

/** 地点卡片节点：图钉 + 名字 + 状态行 + 在场队友。图钉即连线锚点（麻绳钉到钉）。 */
function LocationCard({ data }: NodeProps<Node<CardData>>) {
  const { loc, disabled, onPick } = data
  const clickable = !loc.current && !disabled
  const hStyle = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0, left: '50%', top: 6 }
  const border = loc.current
    ? `2px solid ${CANDLE}`
    : loc.visited ? '1px solid var(--color-border)' : '1px dashed rgba(138,122,92,0.7)'
  return (
    <div
      onClick={() => clickable && onPick(loc)}
      title={loc.current ? '你正在此处' : loc.visited ? '前往（已探索）' : '前往（未曾涉足）'}
      style={{
        width: CARD_W,
        transform: `rotate(${tiltOf(loc.id)}deg)`,
        background: loc.visited || loc.current ? 'var(--color-bg-card)' : 'rgba(26,22,14,0.8)',
        border,
        borderRadius: 4,
        boxShadow: loc.current
          ? `0 0 12px rgba(212,162,78,0.35), 0 3px 6px rgba(0,0,0,0.5)`
          : '0 3px 6px rgba(0,0,0,0.5)',
        opacity: loc.visited || loc.current ? 1 : 0.75,
        cursor: clickable ? 'pointer' : 'default',
        padding: '10px 8px 6px',
        position: 'relative',
      }}
    >
      {/* 连线锚点藏在图钉下：麻绳看起来钉到钉 */}
      <Handle type="source" position={Position.Top} style={hStyle} />
      <Handle type="target" position={Position.Top} style={hStyle} />
      {/* 图钉 */}
      <div style={{
        position: 'absolute', left: '50%', top: -5, marginLeft: -5,
        width: 10, height: 10, borderRadius: '50%',
        background: loc.current
          ? `radial-gradient(circle at 35% 35%, #f0d9a0, ${CANDLE} 60%, #8a6a2e)`
          : 'radial-gradient(circle at 35% 35%, #9a8f7a, #6b6252 60%, #3d372c)',
        boxShadow: '0 2px 3px rgba(0,0,0,0.6)',
      }} />
      <div className="text-xs font-semibold leading-snug" style={{ color: loc.current ? 'var(--color-text-accent)' : PARCH }}>
        {loc.current && <GiPositionMarker size={11} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 2 }} />}
        {loc.name}
      </div>
      <div className="text-[10px] mt-0.5" style={{ color: 'var(--color-text-secondary)' }}>
        {loc.current ? '当前所在' : loc.visited ? '已探索' : '有所耳闻'}
      </div>
      {(loc.party?.length ?? 0) > 0 && !loc.current && (
        <div className="text-[10px] mt-0.5 truncate" style={{ color: CANDLE, opacity: 0.85 }}>
          {loc.party!.join('、')}在此
        </div>
      )}
    </div>
  )
}

const nodeTypes = { card: LocationCard }

function build(locations: BoardLocation[], disabled: boolean, onPick: (loc: BoardLocation) => void): { nodes: Node[]; edges: Edge[] } {
  const ids = new Set(locations.map((l) => l.id))
  const edges: Edge[] = []
  const seen = new Set<string>()
  for (const l of locations) {
    for (const c of l.connections || []) {
      if (!ids.has(c)) continue
      const k = l.id < c ? `${l.id}--${c}` : `${c}--${l.id}`
      if (seen.has(k)) continue
      seen.add(k)
      edges.push({
        id: k, source: l.id, target: c, type: 'default',
        style: { stroke: TWINE, strokeWidth: 1.2, strokeDasharray: '6 4', opacity: 0.65 },
      })
    }
  }
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 34, ranksep: 66, marginx: 26, marginy: 30 })
  g.setDefaultEdgeLabel(() => ({}))
  const cardH = (l: BoardLocation) => 52 + ((l.party?.length ?? 0) > 0 && !l.current ? 16 : 0)
  for (const l of locations) g.setNode(l.id, { width: CARD_W, height: cardH(l) })
  for (const e of edges) g.setEdge(e.source, e.target)
  dagre.layout(g)
  const nodes: Node[] = locations.map((l) => {
    const p = g.node(l.id)
    return {
      id: l.id, type: 'card',
      data: { loc: l, disabled, onPick } as CardData,
      position: { x: p.x - CARD_W / 2, y: p.y - (p.height as number) / 2 },
    }
  })
  return { nodes, edges }
}

export function InvestigationBoard({ locations, disabled, onPick }: {
  locations: BoardLocation[]
  disabled: boolean
  onPick: (loc: BoardLocation) => void
}) {
  const { nodes, edges } = useMemo(() => build(locations, disabled, onPick), [locations, disabled, onPick])
  if (locations.length === 0) {
    return <p className="text-xs py-4 text-center" style={{ color: 'var(--color-text-secondary)' }}>暂无已知的可前往地点。</p>
  }
  return (
    <div style={{
      height: 340, borderRadius: 6, overflow: 'hidden',
      // 暗色软木板/皮革桌面：斜纹肌理 + 中央暖光晕，边缘压暗
      background: [
        'radial-gradient(ellipse at 50% 38%, rgba(212,162,78,0.07) 0%, transparent 58%)',
        'repeating-linear-gradient(115deg, rgba(0,0,0,0.16) 0px, rgba(0,0,0,0.16) 2px, transparent 2px, transparent 7px)',
        'radial-gradient(ellipse at 50% 45%, #241d12 0%, #17120a 78%)',
      ].join(','),
      boxShadow: 'inset 0 0 0 1px rgba(212,162,78,0.16), inset 0 0 42px rgba(0,0,0,0.55)',
    }}>
      <ReactFlowProvider>
        <ReactFlow
          key={locations.map((l) => l.id).join('|')}
          defaultNodes={nodes}
          defaultEdges={edges}
          nodeTypes={nodeTypes}
          nodesConnectable={false}
          fitView
          minZoom={0.4}
          maxZoom={1.6}
          proOptions={{ hideAttribution: true }}
          style={{ background: 'transparent' }}
        />
      </ReactFlowProvider>
    </div>
  )
}
