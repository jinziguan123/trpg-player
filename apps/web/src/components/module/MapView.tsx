import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { User, Box, DoorOpen, Armchair, Eye, Crosshair, ZoomIn, ZoomOut } from 'lucide-react'
import type { ComponentType } from 'react'

export interface TileMap {
  w: number
  h: number
  tiles: string[]
  objects?: { name: string; x: number; y: number; kind?: string; asset_id?: string }[]
  entrances?: { name: string; x: number; y: number; to?: string }[]
  npc_pos?: { name: string; x: number; y: number; hostile?: boolean }[]
  notes?: string
}

/** 运行时叠加的实体（玩家/NPC/敌人/物品当前位置）；不传则只渲染地图自带的物体/NPC/出口。 */
export interface MapEntity { name: string; x: number; y: number; kind: 'player' | 'npc' | 'enemy' | 'item' }

/** 素材库条目（精简）：地图按「类型默认素材 / 显式 asset_id」引用其 image_url 渲染。 */
export interface AssetLite { id: string; kind: string; image_url: string; name?: string }

// 地形字符 → 素材类型
const GLYPH_KIND: Record<string, string> = { '#': 'wall', '.': 'floor', '+': 'door', '~': 'water', ':': 'rubble' }

const TILE = 40
const TILT = 58           // 地面绕 X 轴倾斜角度（俯角），制造透视 2.5D
const PERSP = 1300        // 透视强度（越小近大远小越夸张）
const WALL_H = 34         // 墙体直立高度
const TOKEN_H = 34        // token 直立高度

// 占位色块调色板（与暖色主题协调，近似截图的暖色地面）。换真实 CC0 瓦片图时替换为贴图。
const FLOOR: Record<string, string> = {
  '.': '#cdb487', '+': '#cdb487', '~': '#5f86a6', ':': '#bfa97f', '#': '#b8a079',
}

type IconT = ComponentType<{ size?: number; color?: string }>

/** 直立 billboard：锚定在某格地面、反向抵消地面倾斜 → 立起来正对镜头（透视缩放由浏览器免费给）。
 * 注意：必须是 3D 平面的直接子节点（用 Fragment 渲染、勿包普通 div），否则 3D 变换会被压平、token 平躺。 */
function Billboard({ x, y, h, z = 1, children }: { x: number; y: number; h: number; z?: number; children: React.ReactNode }) {
  return (
    <div style={{
      position: 'absolute',
      left: x * TILE, top: y * TILE + TILE / 2 - h,
      width: TILE, height: h,
      transformOrigin: '50% 100%',
      transform: `translateZ(${z}px) rotateX(-${TILT}deg)`,
      display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
      pointerEvents: 'none',
    }}>
      {children}
    </div>
  )
}

/** 把一张瓦片地图以「透视倾斜地面 + 直立 billboard」的 2.5D 渲染（CSS 3D，浏览器做透视）。滚轮缩放。 */
export function MapView({ map, entities, assets }: { map: TileMap; entities?: MapEntity[]; assets?: AssetLite[] }) {
  const W = map.w, H = map.h
  const planeW = W * TILE, planeH = H * TILE
  const at = (x: number, y: number) => (map.tiles[y] && map.tiles[y][x]) || ' '

  // 素材解析：类型→默认素材（列表按新→旧，取第一个作默认）；id→素材（显式引用优先）。
  const byKind: Record<string, string> = {}
  const byId: Record<string, string> = {}
  for (const a of assets || []) {
    if (!(a.kind in byKind)) byKind[a.kind] = a.image_url
    byId[a.id] = a.image_url
  }
  const spriteFor = (kind?: string, assetId?: string) =>
    (assetId && byId[assetId]) || (kind ? byKind[kind] : undefined)

  // 固定视口 + 内部缩放/平移：滚轮缩放地图本身（容器不变大），可拖拽平移。
  const viewportRef = useRef<HTMLDivElement>(null)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)

  // 进入时按视口大小自适应缩放，并居中
  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const fit = Math.min(1, (el.clientWidth - 24) / sceneW, (el.clientHeight - 24) / sceneH)
    setZoom(fit > 0.1 ? fit : 1)
    setPan({ x: 0, y: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map])

  useEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()  // 在地图上滚轮=缩放地图，不滚动页面、不改容器大小
      setZoom((z) => Math.min(4, Math.max(0.2, z * (e.deltaY < 0 ? 1.12 : 0.89))))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const onMouseDown = (e: React.MouseEvent) => {
    const start = { mx: e.clientX, my: e.clientY, px: pan.x, py: pan.y }
    setDragging(true)
    const move = (ev: MouseEvent) => setPan({ x: start.px + (ev.clientX - start.mx), y: start.py + (ev.clientY - start.my) })
    const up = () => { setDragging(false); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  // 地面瓦片（含墙格的地面底，墙体另画直立板）
  const floors: React.ReactNode[] = []
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const c = at(x, y)
      if (c === ' ') continue
      // 地面：墙格底下铺 floor 素材；其余按字符类型取素材；缺素材回退色块。
      const groundKind = c === '#' ? 'floor' : GLYPH_KIND[c]
      const sprite = spriteFor(groundKind)
      floors.push(
        <div key={`f${x},${y}`} style={{
          position: 'absolute', left: x * TILE, top: y * TILE, width: TILE, height: TILE,
          background: sprite ? `center/100% 100% no-repeat url("${sprite}")` : (FLOOR[c] || FLOOR['.']),
          boxShadow: sprite ? undefined : 'inset 0 0 0 1px rgba(0,0,0,0.07)',
          imageRendering: 'pixelated',
        }}>
          {!sprite && c === '+' && <div style={{ position: 'absolute', inset: '20% 28%', background: '#8a5a2f', borderRadius: 2 }} />}
          {!sprite && c === ':' && <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(rgba(67,56,42,0.5) 1px, transparent 1.5px)', backgroundSize: '9px 9px' }} />}
        </div>,
      )
    }
  }

  // 直立元素：墙板 + token，自上而下排序（远先画、近压上）
  const stand: { y: number; key: string; el: React.ReactNode }[] = []
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (at(x, y) !== '#') continue
      const ws = spriteFor('wall')
      // 最外侧（边界）墙半透明：避免近处外墙挡住内部，方便玩家观察场景
      const isBorder = x === 0 || x === W - 1 || y === 0 || y === H - 1
      stand.push({ y, key: `w${x},${y}`, el: (
        <Billboard x={x} y={y} h={WALL_H}>
          <div style={{ width: TILE, height: WALL_H, imageRendering: 'pixelated', opacity: isBorder ? 0.45 : 1,
            background: ws ? `center/100% 100% no-repeat url("${ws}")` : 'linear-gradient(#7d6c52,#5b4d3a)',
            boxShadow: ws ? undefined : 'inset 0 2px 0 #8e7c60, 0 2px 3px rgba(0,0,0,0.35)' }} />
        </Billboard>
      ) })
    }
  }
  // token：有素材则用素材精灵（直立），否则回退图标圆片。
  const tok = (x: number, y: number, label: string, color: string, Icon: IconT, key: string, kind?: string, assetId?: string) => {
    const sprite = spriteFor(kind, assetId)
    stand.push({
      y, key, el: (
        <Billboard x={x} y={y} h={sprite ? TILE : TOKEN_H} z={2}>
          {sprite ? (
            <div title={label} style={{ width: TILE, height: TILE, imageRendering: 'pixelated', pointerEvents: 'auto', filter: 'drop-shadow(0 2px 2px rgba(0,0,0,0.5))', background: `center bottom/contain no-repeat url("${sprite}")` }} />
          ) : (
            <div title={label} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', pointerEvents: 'auto' }}>
              <div style={{ width: 24, height: 24, borderRadius: '50%', background: color, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 3px rgba(0,0,0,0.5)' }}>
                <Icon size={14} color="#fff" />
              </div>
              <div style={{ width: 2, height: 7, background: 'rgba(0,0,0,0.35)' }} />
            </div>
          )}
        </Billboard>
      ),
    })
  }
  for (const o of map.objects || []) {
    const isItem = o.kind === 'item'
    tok(o.x, o.y, o.name, isItem ? '#b8860b' : '#7a6248', isItem ? Box : o.kind === 'feature' ? Eye : Armchair, `o${o.x},${o.y},${o.name}`, o.kind || 'furniture', o.asset_id)
  }
  for (const e of map.entrances || []) tok(e.x, e.y, e.name, '#2d7d46', DoorOpen, `e${e.x},${e.y},${e.name}`, 'door')
  for (const n of map.npc_pos || []) n.hostile
    ? tok(n.x, n.y, n.name, 'var(--color-danger)', Crosshair, `n${n.x},${n.y},${n.name}`, 'enemy')
    : tok(n.x, n.y, n.name, '#3b6ea5', User, `n${n.x},${n.y},${n.name}`, 'npc')
  for (const en of entities || []) {
    const m = { player: ['var(--color-accent)', Crosshair], npc: ['#3b6ea5', User], enemy: ['var(--color-danger)', Crosshair], item: ['#b8860b', Box] }[en.kind] as [string, IconT]
    tok(en.x, en.y, en.name, m[0], m[1], `en${en.x},${en.y},${en.name}`, en.kind)
  }
  stand.sort((a, b) => a.y - b.y)

  // 场景盒：给透视投影留足横向（近边变宽）与纵向（墙体抬高）余量。
  const sceneW = planeW * 1.35 + 40
  const sceneH = planeH * 0.55 + WALL_H + 60

  return (
    <div
      ref={viewportRef}
      onMouseDown={onMouseDown}
      style={{
        position: 'relative', width: '100%', height: VIEWPORT_H, overflow: 'hidden',
        cursor: dragging ? 'grabbing' : 'grab', userSelect: 'none', touchAction: 'none',
        borderRadius: 6,
      }}
    >
      <button onMouseDown={(e) => e.stopPropagation()} onClick={() => setZoom((z) => Math.max(0.2, z * 0.89))} title="缩小" style={zoomBtn(8)}><ZoomOut size={14} /></button>
      <button onMouseDown={(e) => e.stopPropagation()} onClick={() => setZoom((z) => Math.min(4, z * 1.12))} title="放大" style={zoomBtn(40)}><ZoomIn size={14} /></button>
      {/* 内容层：固定视口内做 平移+缩放，容器尺寸不随缩放变化 */}
      <div style={{
        position: 'absolute', left: '50%', top: '50%',
        width: sceneW, height: sceneH, marginLeft: -sceneW / 2, marginTop: -sceneH / 2,
        transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, transformOrigin: 'center',
      }}>
        <div style={{ position: 'absolute', inset: 0, perspective: `${PERSP}px`, perspectiveOrigin: '50% 46%' }}>
          <div style={{
            position: 'absolute', left: '50%', top: '50%',
            width: planeW, height: planeH, marginLeft: -planeW / 2, marginTop: -planeH / 2,
            transformStyle: 'preserve-3d', transform: `rotateX(${TILT}deg)`,
          }}>
            {floors}
            {stand.map((s) => <Fragment key={s.key}>{s.el}</Fragment>)}
          </div>
        </div>
      </div>
    </div>
  )
}

const VIEWPORT_H = 420   // 地图视口固定高度（缩放只改地图、不改容器）

const zoomBtn = (top: number): React.CSSProperties => ({
  position: 'absolute', right: 8, top, zIndex: 5,
  width: 26, height: 26, borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', color: 'var(--color-text-secondary)',
  cursor: 'pointer',
})
