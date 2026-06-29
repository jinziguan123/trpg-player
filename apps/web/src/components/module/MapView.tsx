import { useEffect, useRef } from 'react'
import { User, Box, DoorOpen, Armchair, Eye, Crosshair } from 'lucide-react'

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

const TILE = 30
const WALL_H = 12         // 墙体抬高的像素，制造伪 2.5D 立体感
const PAD = WALL_H        // 顶部留白，让第 0 行的墙能向上抬

// 占位色块调色板（与暖色主题协调）。换真实 CC0 瓦片图时，这里替换为贴图采样。
const C = {
  floor: '#d9c8a8', floorEdge: '#c9b690',
  wallCap: '#8a7860', wallBody: '#5b4d3a', wallEdge: '#43382a',
  water: '#5b7d8f', waterEdge: '#48626f',
  rubble: '#a89880',
  door: '#b5824a',
  shadow: 'rgba(0,0,0,0.18)',
}

/** 把一张瓦片地图以「俯视 + 墙体抬高」的伪 2.5D 画到 canvas，HTML 层叠 token。 */
export function MapView({ map, entities }: { map: TileMap; entities?: MapEntity[] }) {
  const ref = useRef<HTMLCanvasElement>(null)
  const W = map.w * TILE
  const H = map.h * TILE + PAD

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = W * dpr
    canvas.height = H * dpr
    canvas.style.width = `${W}px`     // CSS 显示尺寸=逻辑尺寸（缓冲区按 dpr 放大保清晰），否则高 dpr 屏会画大一倍、token 错位
    canvas.style.height = `${H}px`
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.imageSmoothingEnabled = false
    ctx.clearRect(0, 0, W, H)

    const cellTop = (y: number) => PAD + y * TILE
    const at = (x: number, y: number) => (map.tiles[y] && map.tiles[y][x]) || ' '

    const drawFloor = (x: number, y: number, fill: string, edge: string) => {
      const px = x * TILE, py = cellTop(y)
      ctx.fillStyle = fill
      ctx.fillRect(px, py, TILE, TILE)
      ctx.strokeStyle = edge
      ctx.lineWidth = 1
      ctx.strokeRect(px + 0.5, py + 0.5, TILE - 1, TILE - 1)
    }

    // 先铺地面（地板/门/水/碎石），墙后画以便正确遮挡
    for (let y = 0; y < map.h; y++) {
      for (let x = 0; x < map.w; x++) {
        const c = at(x, y)
        if (c === '#' || c === ' ') continue
        if (c === '~') drawFloor(x, y, C.water, C.waterEdge)
        else if (c === ':') {
          drawFloor(x, y, C.rubble, C.floorEdge)
          ctx.fillStyle = C.wallEdge
          for (const [dx, dy] of [[6, 8], [18, 6], [12, 20], [22, 22]]) ctx.fillRect(x * TILE + dx, cellTop(y) + dy, 3, 3)
        } else {
          drawFloor(x, y, C.floor, C.floorEdge)
          if (c === '+') { // 门：在地板上画一道开口
            ctx.fillStyle = C.door
            ctx.fillRect(x * TILE + 6, cellTop(y) + 4, TILE - 12, TILE - 8)
            ctx.fillStyle = C.floor
            ctx.fillRect(x * TILE + 10, cellTop(y) + 8, TILE - 20, TILE - 16)
          }
        }
      }
    }

    // 再画墙：自上而下，墙体向上抬 WALL_H，近处（下方）的墙遮挡远处（上方）
    for (let y = 0; y < map.h; y++) {
      for (let x = 0; x < map.w; x++) {
        if (at(x, y) !== '#') continue
        const px = x * TILE, py = cellTop(y)
        // 地面投影阴影（朝下）
        if (at(x, y + 1) !== '#') {
          ctx.fillStyle = C.shadow
          ctx.fillRect(px, py + TILE, TILE, 5)
        }
        // 墙身（从抬高的顶到本格底）
        ctx.fillStyle = C.wallBody
        ctx.fillRect(px, py - WALL_H, TILE, TILE + WALL_H)
        // 顶面（抬高的盖）
        ctx.fillStyle = C.wallCap
        ctx.fillRect(px, py - WALL_H, TILE, WALL_H)
        ctx.strokeStyle = C.wallEdge
        ctx.lineWidth = 1
        ctx.strokeRect(px + 0.5, py - WALL_H + 0.5, TILE - 1, TILE + WALL_H - 1)
      }
    }
  }, [map, W, H])

  // token：HTML 层绝对定位叠在 canvas 上（文字/图标/悬停名比 canvas 内画更省事）
  const tokens: { x: number; y: number; label: string; color: string; Icon: typeof User }[] = []
  for (const o of map.objects || []) {
    const isItem = o.kind === 'item'
    tokens.push({ x: o.x, y: o.y, label: o.name, color: isItem ? '#b8860b' : '#7a6248', Icon: isItem ? Box : o.kind === 'feature' ? Eye : Armchair })
  }
  for (const e of map.entrances || []) tokens.push({ x: e.x, y: e.y, label: e.name, color: '#2d7d46', Icon: DoorOpen })
  for (const n of map.npc_pos || []) tokens.push({ x: n.x, y: n.y, label: n.name, color: '#3b6ea5', Icon: User })
  for (const en of entities || []) {
    const m = { player: { c: 'var(--color-accent)', I: Crosshair }, npc: { c: '#3b6ea5', I: User }, enemy: { c: 'var(--color-danger)', I: Crosshair }, item: { c: '#b8860b', I: Box } }[en.kind]
    tokens.push({ x: en.x, y: en.y, label: en.name, color: m.c, Icon: m.I })
  }

  return (
    <div style={{ position: 'relative', width: W, height: H, imageRendering: 'pixelated' }}>
      <canvas ref={ref} style={{ display: 'block', imageRendering: 'pixelated' }} />
      {tokens.map((t, i) => (
        <div key={i} title={t.label}
          className="absolute flex items-center justify-center"
          style={{ left: t.x * TILE, top: PAD + t.y * TILE, width: TILE, height: TILE, pointerEvents: 'none' }}>
          <div className="flex items-center justify-center rounded-full"
            style={{ width: 20, height: 20, background: t.color, boxShadow: '0 1px 2px rgba(0,0,0,0.4)' }}>
            <t.Icon size={12} color="#fff" />
          </div>
        </div>
      ))}
    </div>
  )
}
