import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Stage, Layer, Group, RegularPolygon, Line, Rect, Circle, Text, Shape } from 'react-konva'
import type { KonvaEventObject } from 'konva/lib/Node'
import { terrainGeometryKey } from '@/lib/terrain'
import { BIOME_TEXTURES } from '@/lib/biome'

/** 沙盘节点。sceneId 为空时是普通地貌节点；两者使用完全相同的六边形底图。 */
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
  sceneId?: string
  nodeKind?: 'scene' | 'terrain'
}

interface Props {
  locations: SandboxLocation[]
  disabled: boolean
  onPick?: (loc: SandboxLocation) => void
  editable?: boolean
  /** 兼容旧调用名；编辑时对普通节点和场景节点都生效。 */
  onMoveScene?: (id: string, q: number, r: number) => void
  onMoveNode?: (id: string, q: number, r: number) => void
  selectedIds?: readonly string[]
  onToggleScene?: (id: string) => void
  /** 真人 KP 上帝视角可显式查看尚未发现的场景 token。 */
  revealUnknownTokens?: boolean
  /** 编辑模式下双击空白处新增地形节点 */
  onAddNode?: (q: number, r: number) => void
  /** 从地貌面板拖入沙盘新增节点 */
  onDropBiome?: (q: number, r: number, biome: string) => void
  /** 节点被拖出沙盘边界时触发删除 */
  onDeleteNode?: (id: string) => void
  height?: string
}

const R = 42
const SQRT3 = Math.sqrt(3)
const CANDLE = '#e6b85c'
const PARCH = '#f0e2c2'
const THREAD = '#d05353'
const EDGE_DARK = 'rgba(16, 11, 7, 0.94)'
const EDGE_LIGHT = '#f0cb74'

const hexXY = (q: number, r: number) => ({ x: R * SQRT3 * (q + r / 2), y: R * 1.5 * r })

function xyToHex(x: number, y: number) {
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

const BIOME: Record<string, { fill: string; stroke: string; shade: string }> = {
  plain: { fill: '#8a9b5b', stroke: '#c6d58a', shade: '#53643a' },
  forest: { fill: '#2f6b46', stroke: '#8fc46f', shade: '#173d2b' },
  water: { fill: '#2f78a6', stroke: '#8bd0e5', shade: '#174968' },
  coast: { fill: '#4d91a2', stroke: '#e4d19a', shade: '#d6bb72' },
  desert: { fill: '#b08a4f', stroke: '#edcf8b', shade: '#76572d' },
  mountain: { fill: '#6b6870', stroke: '#c6c5c2', shade: '#34343a' },
  swamp: { fill: '#4e6541', stroke: '#a7bc65', shade: '#293a2b' },
  urban: { fill: '#806f67', stroke: '#d5b49e', shade: '#4a3935' },
  ruin: { fill: '#71665f', stroke: '#c8b3a2', shade: '#3d3632' },
  interior: { fill: '#8f7658', stroke: '#e1c38b', shade: '#4c3828' },
  road: { fill: '#876747', stroke: '#f0cf92', shade: '#5f4430' },
}
const biomeOf = (b?: string) => BIOME[(b || '').toLowerCase()] || BIOME.plain

type TerrainTexture = HTMLImageElement | undefined
const texturePatternCache = new WeakMap<CanvasRenderingContext2D, WeakMap<HTMLImageElement, CanvasPattern>>()
const texturePattern = (ctx: CanvasRenderingContext2D, image: HTMLImageElement) => {
  let byImage = texturePatternCache.get(ctx)
  if (!byImage) {
    byImage = new WeakMap()
    texturePatternCache.set(ctx, byImage)
  }
  const cached = byImage.get(image)
  if (cached) return cached
  const pattern = ctx.createPattern(image, 'repeat')
  if (pattern) {
    if (typeof DOMMatrix !== 'undefined') pattern.setTransform(new DOMMatrix().scale(0.34))
    byImage.set(image, pattern)
  }
  return pattern
}

/** 地貌六角格：纯色打底 + 纹理叠加，不再绘制程序化符号。
 *  命中检测由父级 Group 中的透明 RegularPolygon 负责，本 Shape 不监听事件。 */
const TerrainTile = memo(function TerrainTile({ biome, stroke, strokeWidth, opacity, texture }: {
  biome: string; stroke: string; strokeWidth: number; opacity: number; texture?: TerrainTexture
}) {
  const style = biomeOf(biome)
  return <Shape
    listening={false}
    perfectDrawEnabled={false}
    shadowForStrokeEnabled={false}
    opacity={opacity}
    sceneFunc={(context, shape) => {
      const ctx = (context as unknown as { _context: CanvasRenderingContext2D })._context
      const hex = Array.from({ length: 6 }, (_, i) => {
        const angle = Math.PI / 3 * i - Math.PI / 2
        return [Math.cos(angle) * (R - 1), Math.sin(angle) * (R - 1)] as const
      })
      ctx.beginPath(); ctx.moveTo(hex[0][0], hex[0][1])
      hex.slice(1).forEach(([x, y]) => ctx.lineTo(x, y)); ctx.closePath()
      context.fillStrokeShape(shape)
      if (texture?.complete && texture.naturalWidth > 0) {
        const pattern = texturePattern(ctx, texture)
        if (pattern) {
          ctx.save()
          ctx.beginPath(); ctx.moveTo(hex[0][0], hex[0][1])
          hex.slice(1).forEach(([x, y]) => ctx.lineTo(x, y)); ctx.closePath(); ctx.clip()
          ctx.globalAlpha = 0.55
          ctx.fillStyle = pattern
          ctx.fillRect(-R, -R, R * 2, R * 2)
          ctx.restore()
        }
      }
    }}
    fill={style.fill}
    stroke={stroke}
    strokeWidth={strokeWidth}
  />
})

const DANGER_COLORS: Record<string, string> = { uneasy: '#e5c84c', dangerous: '#e8874c', deadly: '#e25b5b' }

export function HexSandbox({
  locations, disabled, onPick, editable, onMoveScene, onMoveNode,
  selectedIds = [], onToggleScene, revealUnknownTokens = false,
  onAddNode, onDropBiome, onDeleteNode,
  height = 'clamp(320px, 58vh, 560px)',
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 640, h: 420 })
  const [dragOver, setDragOver] = useState(false)
  const [textures, setTextures] = useState<Record<string, HTMLImageElement>>({})
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.Image === 'undefined') return
    let active = true
    const loads = [...new Set(Object.values(BIOME_TEXTURES))].map((url) => new Promise<[string, HTMLImageElement]>((resolve) => {
      const image = new window.Image()
      image.decoding = 'async'
      image.onload = () => resolve([url, image])
      image.onerror = () => resolve([url, image])
      image.src = url
    }))
    Promise.all(loads).then((loaded) => {
      if (!active) return
      const byUrl = Object.fromEntries(loaded.filter(([, image]) => image.naturalWidth > 0))
      setTextures(Object.fromEntries(Object.entries(BIOME_TEXTURES)
        .map(([biome, url]) => [biome, byUrl[url]])
        .filter(([, image]) => image)))
    })
    return () => { active = false }
  }, [])
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    return () => ro.disconnect()
  }, [])

  const located = useMemo(() => locations.filter((l) => l.map && Number.isFinite(l.map.q) && Number.isFinite(l.map.r)), [locations])
  const scenes = useMemo(() => located.filter((l) => l.nodeKind !== 'terrain' && l.sceneId !== undefined), [located])
  const selected = useMemo(() => new Set(selectedIds), [selectedIds])
  const fit = useMemo(() => {
    if (!located.length) return { x: size.w / 2, y: size.h / 2, scale: 1 }
    const pts = located.map((l) => hexXY(l.map!.q, l.map!.r))
    const minX = Math.min(...pts.map((p) => p.x)) - R * 3
    const maxX = Math.max(...pts.map((p) => p.x)) + R * 3
    const minY = Math.min(...pts.map((p) => p.y)) - R * 3
    const maxY = Math.max(...pts.map((p) => p.y)) + R * 3
    const scale = Math.min(size.w / Math.max(1, maxX - minX), size.h / Math.max(1, maxY - minY), 1.15)
    return { scale, x: size.w / 2 - ((minX + maxX) / 2) * scale, y: size.h / 2 - ((minY + maxY) / 2) * scale }
  }, [located, size])
  const [view, setView] = useState(fit)
  const geometryKey = terrainGeometryKey(located.map((location) => ({
    id: location.id,
    q: location.map!.q,
    r: location.map!.r,
  })), size.w, size.h)
  const fittedGeometryRef = useRef('')
  useEffect(() => {
    if (fittedGeometryRef.current === geometryKey) return
    fittedGeometryRef.current = geometryKey
    setView(fit)
  }, [fit, geometryKey])

  const byId = useMemo(() => new Map(scenes.map((l) => [l.id, l])), [scenes])
  const edges = useMemo(() => {
    const seen = new Set<string>(); const out: { a: SandboxLocation; b: SandboxLocation }[] = []
    for (const l of scenes) for (const cid of l.connections || []) {
      const other = byId.get(cid); if (!other) continue
      const key = [l.id, other.id].sort().join('|'); if (seen.has(key)) continue
      seen.add(key); out.push({ a: l, b: other })
    }
    return out
  }, [scenes, byId])

  /** 从屏幕坐标反算六角格坐标 */
  const screenToHex = (screenX: number, screenY: number) => {
    const rawX = (screenX - view.x) / view.scale
    const rawY = (screenY - view.y) / view.scale
    return xyToHex(rawX, rawY)
  }

  const onStageDblClick = (e: KonvaEventObject<MouseEvent>) => {
    if (!editable || !onAddNode) return
    if (e.target !== e.currentTarget) return
    const stage = e.target.getStage()
    if (!stage) return
    const pointer = stage.getPointerPosition()
    if (!pointer) return
    const hex = screenToHex(pointer.x, pointer.y)
    const occupied = new Set(located.map((l) => `${l.map?.q},${l.map?.r}`))
    if (!occupied.has(`${hex.q},${hex.r}`)) {
      onAddNode(hex.q, hex.r)
    }
  }

  // ── 从地貌面板拖入沙盘 ──
  const handleDragOver = (e: React.DragEvent) => {
    if (!editable || !onDropBiome) return
    if (!e.dataTransfer.types.includes('application/x-sandbox-biome')) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDragOver(true)
  }
  const handleDragLeave = (e: React.DragEvent) => {
    if (e.currentTarget === e.target || !(e.currentTarget as HTMLElement).contains(e.relatedTarget as HTMLElement)) {
      setDragOver(false)
    }
  }
  const handleDrop = (e: React.DragEvent) => {
    setDragOver(false)
    if (!editable || !onDropBiome) return
    e.preventDefault()
    const biome = e.dataTransfer.getData('application/x-sandbox-biome')
    if (!biome) return
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const screenX = e.clientX - rect.left
    const screenY = e.clientY - rect.top
    const hex = screenToHex(screenX, screenY)
    const occupied = new Set(located.map((l) => `${l.map?.q},${l.map?.r}`))
    if (!occupied.has(`${hex.q},${hex.r}`)) {
      onDropBiome(hex.q, hex.r, biome)
    }
  }

  const onWheel = (e: KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault(); const stage = e.target.getStage(); if (!stage) return
    const pointer = stage.getPointerPosition(); if (!pointer) return
    const old = view.scale; const next = Math.min(2.8, Math.max(0.35, old * (e.evt.deltaY > 0 ? 0.9 : 1.1)))
    setView({ scale: next, x: pointer.x - ((pointer.x - view.x) / old) * next, y: pointer.y - ((pointer.y - view.y) / old) * next })
  }

  return <div ref={wrapRef} style={{ height, borderRadius: 8, overflow: 'hidden', position: 'relative', border: dragOver ? '2px dashed var(--color-accent)' : '1px solid var(--color-border)' }}
    onDragOver={handleDragOver}
    onDragLeave={handleDragLeave}
    onDrop={handleDrop}>
    {dragOver && <div className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none" style={{ background: 'color-mix(in srgb, var(--color-accent) 12%, transparent)' }}>
      <span className="text-sm font-semibold px-3 py-1.5 rounded" style={{ background: 'var(--color-accent)', color: 'var(--color-on-accent)' }}>放置地貌节点</span>
    </div>}
    <Stage width={size.w} height={size.h} onWheel={onWheel} onDblClick={onStageDblClick} draggable x={view.x} y={view.y} scaleX={view.scale} scaleY={view.scale}
      onDragEnd={(e: KonvaEventObject<DragEvent>) => { const s = e.target.getStage(); if (s === e.target.getStage() && e.target.getClassName() === 'Stage') setView((v) => ({ ...v, x: s!.x(), y: s!.y() })) }}
      style={{ background: 'radial-gradient(ellipse 80% 70% at 50% 45%, #2a2418 0%, #17130d 68%, #0b0906 100%)', cursor: 'grab' }}>
      <Layer>
        {located.map((l) => {
          const p = hexXY(l.map!.q, l.map!.r); const style = biomeOf(l.map!.biome); const isScene = l.nodeKind !== 'terrain' && !!l.sceneId
          const unknown = l.known === false; const isSelected = editable && selected.has(l.id); const active = l.current || isSelected
          const showToken = isScene && (!unknown || revealUnknownTokens)
          const clickable = !!onPick && isScene && !l.current && !disabled && !unknown
          const move = (id: string, q: number, r: number) => (onMoveNode || onMoveScene)?.(id, q, r)
          return <Group key={l.id} x={p.x} y={p.y} draggable={!!editable}
            onDragEnd={(e: KonvaEventObject<DragEvent>) => {
              const node = e.target
              const nx = node.x(), ny = node.y()
              // 拖出沙盘边界（超出舞台可视范围过多）→ 删除
              const stageEl = node.getStage()
              if (stageEl && onDeleteNode) {
                const sw = stageEl.width(), sh = stageEl.height()
                const margin = R * 4
                if (nx < -margin || ny < -margin || nx > sw + margin || ny > sh + margin) {
                  node.position(p)
                  onDeleteNode(l.id)
                  return
                }
              }
              const next = xyToHex(nx, ny); node.position(p); move(l.id, next.q, next.r)
            }}
            onClick={() => editable ? onToggleScene?.(l.id) : clickable && onPick?.(l)}
            onTap={() => editable ? onToggleScene?.(l.id) : clickable && onPick?.(l)}
            onMouseEnter={(e) => { const st = e.target.getStage(); if (st) st.container().style.cursor = editable ? 'move' : clickable ? 'pointer' : 'grab' }}
            onMouseLeave={(e) => { const st = e.target.getStage(); if (st) st.container().style.cursor = 'grab' }}>
            {/* 六边形命中检测区域：透明、始终监听，确保父 Group 的事件正确触发 */}
            <RegularPolygon sides={6} radius={R - 2} fill="transparent" listening={true} perfectDrawEnabled={false} />
            <TerrainTile biome={l.map!.biome}
              stroke={active ? CANDLE : style.stroke} strokeWidth={active ? 3 : isScene ? 2 : 1.3}
              opacity={0.97}
              texture={textures[(l.map!.biome || 'plain').toLowerCase()]} />
            {showToken && <RegularPolygon sides={6} radius={R - 8} stroke={DANGER_COLORS[l.danger || ''] || 'rgba(245,230,190,0.65)'} strokeWidth={1.2} opacity={0.85} listening={false} perfectDrawEnabled={false} />}
            {showToken && <Group listening={false}>
              <Circle y={-3} radius={14} fill="#242019" stroke={active ? CANDLE : '#f1d18a'} strokeWidth={2} shadowColor={CANDLE} shadowBlur={active ? 8 : 0} />
              <Line points={[0, 9, -6, 17, 6, 17]} closed fill="#242019" stroke={active ? CANDLE : '#f1d18a'} strokeWidth={1.5} perfectDrawEnabled={false} />
              <Text text={(l.name || l.id).slice(0, 1)} x={-9} y={-9} width={18} align="center" fontSize={13} fontStyle="bold" fill={PARCH} />
            </Group>}
            {showToken && (l.party || []).slice(0, 3).map((name, i) => <Group key={name + i} x={R * 0.62 - i * 15} y={-R * 0.78} listening={false}><Circle radius={8} fill="#29221b" stroke={CANDLE} strokeWidth={1} /><Text text={name.slice(0, 1)} x={-8} y={-5.5} width={16} align="center" fontSize={10} fill={PARCH} /></Group>)}
            {showToken && (l.clues?.length || 0) > 0 && <Group x={-R * 0.66} y={-R * 0.78} listening={false}><Circle radius={8} fill={THREAD} /><Text text={String(l.clues!.length)} x={-8} y={-5.5} width={16} align="center" fontSize={10} fontStyle="bold" fill={PARCH} /></Group>}
          </Group>
        })}
        {edges.map(({ a, b }) => {
          const pa = hexXY(a.map!.q, a.map!.r)
          const pb = hexXY(b.map!.q, b.map!.r)
          const dx = pb.x - pa.x
          const dy = pb.y - pa.y
          const distance = Math.max(1, Math.hypot(dx, dy))
          const inset = Math.min(R * 0.82, distance * 0.28)
          const ux = dx / distance
          const uy = dy / distance
          const points = [pa.x + ux * inset, pa.y + uy * inset, pb.x - ux * inset, pb.y - uy * inset]
          const active = selected.has(a.id) || selected.has(b.id)
          return <Group key={`e-${a.id}-${b.id}`} listening={false}>
            <Line points={points} stroke={EDGE_DARK} strokeWidth={active ? 12 : 9} lineCap="round" opacity={0.92} perfectDrawEnabled={false} />
            <Line points={points} stroke={active ? CANDLE : EDGE_LIGHT} strokeWidth={active ? 4 : 3} dash={[14, 8]} lineCap="round" opacity={0.98} perfectDrawEnabled={false} />
            <Circle x={points[0]} y={points[1]} radius={active ? 4 : 3} fill={active ? CANDLE : EDGE_LIGHT} stroke={EDGE_DARK} strokeWidth={1.5} perfectDrawEnabled={false} />
            <Circle x={points[2]} y={points[3]} radius={active ? 4 : 3} fill={active ? CANDLE : EDGE_LIGHT} stroke={EDGE_DARK} strokeWidth={1.5} perfectDrawEnabled={false} />
          </Group>
        })}
        {scenes.filter((l) => l.known !== false || revealUnknownTokens).map((l) => { const p = hexXY(l.map!.q, l.map!.r); const isSelected = editable && selected.has(l.id); const nameW = Math.max(52, (l.name || l.id).length * 13 + 16); return <Group key={`label-${l.id}`} x={p.x} y={p.y} listening={false}>
          <Rect x={-nameW / 2} y={R - 2} width={nameW} height={20} cornerRadius={3} fill="rgba(26,21,13,0.9)" stroke={l.current || isSelected ? CANDLE : 'rgba(221,188,122,0.7)'} strokeWidth={l.current || isSelected ? 1.5 : 0.8} />
          <Text text={l.name || l.id} x={-nameW / 2} y={R + 3} width={nameW} align="center" fontSize={12} fontStyle={l.current ? 'bold' : 'normal'} fill={l.current ? CANDLE : PARCH} />
        </Group> })}
      </Layer>
    </Stage>
    {!located.length && <div className="absolute inset-0 flex items-center justify-center text-xs" style={{ color: 'var(--color-text-secondary)', pointerEvents: 'none' }}>尚无可显示的沙盘节点</div>}
  </div>
}
