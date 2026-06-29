import { User, Box, DoorOpen, Armchair, Eye, Crosshair } from 'lucide-react'
import type { ComponentType } from 'react'

export interface TileMap {
  w: number
  h: number
  tiles: string[]
  objects?: { name: string; x: number; y: number; kind?: string }[]
  entrances?: { name: string; x: number; y: number; to?: string }[]
  npc_pos?: { name: string; x: number; y: number }[]
  notes?: string
}

/** 运行时叠加的实体（玩家/NPC/敌人/物品当前位置）；不传则只渲染地图自带的物体/NPC/出口。 */
export interface MapEntity { name: string; x: number; y: number; kind: 'player' | 'npc' | 'enemy' | 'item' }

const TILE = 36
const TILT = 60           // 地面绕 X 轴倾斜角度（俯角），制造透视 2.5D
const PERSP = 1100        // 透视强度（越小近大远小越夸张）
const WALL_H = 30         // 墙体直立高度
const TOKEN_H = 30        // token 直立高度

// 占位色块调色板（与暖色主题协调，近似截图的暖色地面）。换真实 CC0 瓦片图时替换为贴图。
const FLOOR: Record<string, string> = {
  '.': '#cdb487', '+': '#cdb487', '~': '#5f86a6', ':': '#bfa97f', '#': '#b8a079',
}

type IconT = ComponentType<{ size?: number; color?: string }>

/** 直立 billboard：锚定在某格地面、反向抵消地面倾斜 → 立起来正对镜头（透视缩放由浏览器免费给）。 */
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

/** 把一张瓦片地图以「透视倾斜地面 + 直立 billboard」的 2.5D 渲染（CSS 3D，浏览器做透视）。 */
export function MapView({ map, entities }: { map: TileMap; entities?: MapEntity[] }) {
  const W = map.w, H = map.h
  const planeW = W * TILE, planeH = H * TILE
  const at = (x: number, y: number) => (map.tiles[y] && map.tiles[y][x]) || ' '

  // 地面瓦片（含墙格的地面底，墙体另画直立板）
  const floors: React.ReactNode[] = []
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const c = at(x, y)
      if (c === ' ') continue
      floors.push(
        <div key={`f${x},${y}`} style={{
          position: 'absolute', left: x * TILE, top: y * TILE, width: TILE, height: TILE,
          background: FLOOR[c] || FLOOR['.'],
          boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.07)',
          imageRendering: 'pixelated',
        }}>
          {c === '+' && <div style={{ position: 'absolute', inset: '20% 28%', background: '#8a5a2f', borderRadius: 2 }} />}
          {c === ':' && <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(rgba(67,56,42,0.5) 1px, transparent 1.5px)', backgroundSize: '8px 8px' }} />}
        </div>,
      )
    }
  }

  // 直立元素：墙板 + 物体/出口/NPC/实体 token
  const stand: { x: number; y: number; el: React.ReactNode; key: string }[] = []
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (at(x, y) !== '#') continue
      stand.push({
        x, y, key: `w${x},${y}`,
        el: (
          <Billboard x={x} y={y} h={WALL_H}>
            <div style={{ width: TILE, height: WALL_H, background: 'linear-gradient(#7d6c52,#5b4d3a)', boxShadow: 'inset 0 2px 0 #8e7c60, 0 2px 3px rgba(0,0,0,0.35)' }} />
          </Billboard>
        ),
      })
    }
  }

  const tok = (x: number, y: number, label: string, color: string, Icon: IconT, key: string) => ({
    x, y, key,
    el: (
      <Billboard x={x} y={y} h={TOKEN_H} z={2}>
        <div title={label} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', pointerEvents: 'auto' }}>
          <div style={{ width: 22, height: 22, borderRadius: '50%', background: color, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 3px rgba(0,0,0,0.5)' }}>
            <Icon size={13} color="#fff" />
          </div>
          <div style={{ width: 2, height: 6, background: 'rgba(0,0,0,0.35)' }} />
        </div>
      </Billboard>
    ),
  })
  for (const o of map.objects || []) {
    const isItem = o.kind === 'item'
    stand.push(tok(o.x, o.y, o.name, isItem ? '#b8860b' : '#7a6248', isItem ? Box : o.kind === 'feature' ? Eye : Armchair, `o${o.x},${o.y},${o.name}`))
  }
  for (const e of map.entrances || []) stand.push(tok(e.x, e.y, e.name, '#2d7d46', DoorOpen, `e${e.x},${e.y},${e.name}`))
  for (const n of map.npc_pos || []) stand.push(tok(n.x, n.y, n.name, '#3b6ea5', User, `n${n.x},${n.y},${n.name}`))
  for (const en of entities || []) {
    const m = { player: ['var(--color-accent)', Crosshair], npc: ['#3b6ea5', User], enemy: ['var(--color-danger)', Crosshair], item: ['#b8860b', Box] }[en.kind] as [string, IconT]
    stand.push(tok(en.x, en.y, en.name, m[0], m[1], `en${en.x},${en.y},${en.name}`))
  }
  // 自上而下排序：远（y 小）的先画，近的后画压在上面
  stand.sort((a, b) => a.y - b.y)

  return (
    <div style={{
      position: 'relative',
      perspective: `${PERSP}px`, perspectiveOrigin: '50% 42%',
      width: planeW, maxWidth: '100%', height: planeH * 0.6 + WALL_H + 40,
      margin: '0 auto', overflow: 'hidden',
    }}>
      <div style={{
        position: 'absolute', left: '50%', top: '52%',
        width: planeW, height: planeH, marginLeft: -planeW / 2, marginTop: -planeH / 2,
        transformStyle: 'preserve-3d', transform: `rotateX(${TILT}deg)`,
      }}>
        {floors}
        {stand.map((s) => <div key={s.key}>{s.el}</div>)}
      </div>
    </div>
  )
}
