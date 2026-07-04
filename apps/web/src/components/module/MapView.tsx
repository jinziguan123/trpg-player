import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Box, DoorOpen, ZoomIn, ZoomOut } from 'lucide-react'
import {
  GiBed, GiDesk, GiRoundTable, GiWoodenChair, GiWindow, GiLockers, GiBookshelf,
  GiRolledCloth, GiChest, GiFireplace, GiStairs, GiClosedDoors, GiCandleFlame,
  GiWell, GiCoffin, GiSkeleton, GiOpenBook, GiMirrorMirror, GiPaintedPottery,
  GiBathtub, GiBarrel, GiSofa, GiPianoKeys, GiFlowerPot, GiKey, GiStonePile,
  GiSwapBag, GiMagnifyingGlass,
} from 'react-icons/gi'
import type { ComponentType } from 'react'

export interface TileMap {
  w: number
  h: number
  tiles: string[]
  objects?: { name: string; x: number; y: number; kind?: string; asset_id?: string }[]
  entrances?: { name: string; x: number; y: number; to?: string; asset_id?: string }[]
  npc_pos?: { name: string; x: number; y: number; hostile?: boolean; asset_id?: string }[]
  tile_assets?: Record<string, string>   // 逐格地形素材覆盖："x,y" → asset_id（不填则按类型默认）
  notes?: string
}

/** 运行时叠加的实体（玩家/NPC/敌人/物品当前位置）；不传则只渲染地图自带的物体/NPC/出口。
 *  active: 当前行动者（呼吸光圈）；不传时默认玩家 token 发烛光。 */
export interface MapEntity { name: string; x: number; y: number; kind: 'player' | 'ally' | 'npc' | 'enemy' | 'item'; asset_id?: string; active?: boolean }

/** 素材库条目（精简）：地图按「类型默认素材 / 显式 asset_id」引用其 image_url 渲染。 */
export interface AssetLite { id: string; kind: string; image_url: string; name?: string }

// 地形字符 → 素材类型
const GLYPH_KIND: Record<string, string> = { '#': 'wall', '.': 'floor', '+': 'door', '~': 'water', ':': 'rubble' }

const TILE = 40
const TILT = 58           // 地面绕 X 轴倾斜角度（俯角），制造透视 2.5D
const PERSP = 1300        // 透视强度（越小近大远小越夸张）
const WALL_H = 34         // 墙体直立高度
const TOKEN_H = 34        // token 直立高度

// ── 深夜暖光调色板：烛光下的羊皮纸探索图 ──────────────────────────────
// 基底压暗（贴近 #0c0e13），烛光琥珀 #d4a24e 做光源与强调，羊皮纸 #e8dcc0 做文字。
const CANDLE = '212,162,78'      // 烛光琥珀 rgb
const PARCH = '#e8dcc0'          // 羊皮纸暖白
// 地面色块（无素材时）：[主色, 交错色] 轻微棋盘变化替代格线
const TILE_BG: Record<string, [string, string]> = {
  '.': ['#3e3524', '#39301f'],   // 地板：暗羊皮纸褐
  '+': ['#3e3524', '#39301f'],   // 门格地面同地板，门板另画
  '~': ['#20363f', '#1c313a'],   // 水面：深青
  ':': ['#342c1c', '#2f2818'],   // 碎石：更暗的土褐
  '#': ['#332c1c', '#332c1c'],   // 墙格地面（基本被墙板盖住）
}

type IconT = ComponentType<{ size?: number; color?: string }>

// ── 物体图标：按名称关键词选形（先长词后短词，避免「书柜」落进「柜」之前先命中「书」）──
// 命中不了再按 kind 兜底（furniture/item/feature 三档）。全部 game-icons 矢量，禁 emoji。
const OBJ_ICON_RULES: [RegExp, IconT][] = [
  [/书架|书柜/, GiBookshelf],
  [/书桌|办公桌|写字台/, GiDesk],
  [/床|榻/, GiBed],
  [/椅|凳/, GiWoodenChair],
  [/沙发/, GiSofa],
  [/桌|台案|案/, GiRoundTable],
  [/窗/, GiWindow],
  [/柜|橱/, GiLockers],
  [/地毯|挂毯|毯/, GiRolledCloth],
  [/箱|盒|匣/, GiChest],
  [/壁炉|火炉|炉/, GiFireplace],
  [/楼梯|阶梯|台阶/, GiStairs],
  [/门/, GiClosedDoors],
  [/灯|烛|油灯/, GiCandleFlame],
  [/井/, GiWell],
  [/棺/, GiCoffin],
  [/骸骨|尸体|骷髅|遗骸/, GiSkeleton],
  [/书|日记|笔记|信|卷轴|手稿|文件|档案/, GiOpenBook],
  [/镜/, GiMirrorMirror],
  [/瓶|罐|坛|陶/, GiPaintedPottery],
  [/浴缸|水池/, GiBathtub],
  [/桶/, GiBarrel],
  [/钢琴|琴/, GiPianoKeys],
  [/花|盆栽|植物/, GiFlowerPot],
  [/钥匙/, GiKey],
  [/雕像|石像|神像|石碑|祭坛/, GiStonePile],
]
const KIND_FALLBACK_ICON: Record<string, IconT> = {
  item: GiSwapBag, feature: GiMagnifyingGlass, furniture: GiRoundTable,
}
function objIcon(name: string, kind?: string): IconT {
  for (const [re, icon] of OBJ_ICON_RULES) if (re.test(name)) return icon
  return KIND_FALLBACK_ICON[kind || 'furniture'] || Box
}

/** 名字 → 稳定色相（同名恒同色）：给无素材的 NPC 一个可区分的私有色环基底。 */
function nameHue(name: string): number {
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) | 0
  return ((h % 360) + 360) % 360
}

/** 直立 billboard：锚定在某格地面、反向抵消地面倾斜 → 立起来正对镜头（透视缩放由浏览器免费给）。
 * 注意：必须是 3D 平面的直接子节点（用 Fragment 渲染、勿包普通 div），否则 3D 变换会被压平、token 平躺。 */
function Billboard({ x, y, h, z = 1, dx = 0, tilt = TILT, children }: { x: number; y: number; h: number; z?: number; dx?: number; tilt?: number; children: React.ReactNode }) {
  return (
    <div style={{
      position: 'absolute',
      left: x * TILE + dx, top: y * TILE + TILE / 2 - h,
      width: TILE, height: h,
      transformOrigin: '50% 100%',
      transform: `translateZ(${z}px) rotateX(-${tilt}deg)`,
      display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
      pointerEvents: 'none',
    }}>
      {children}
    </div>
  )
}

/** 光照/迷雾叠加层：warm=烛光暖染，dark=离光源远处压暗；extra 放门的琥珀微光等附加层。 */
function shade(dark: number, warm: number, extra?: string) {
  const layers: string[] = []
  if (extra) layers.push(extra)
  if (warm > 0.01) layers.push(`linear-gradient(rgba(${CANDLE},${warm.toFixed(3)}), rgba(${CANDLE},${warm.toFixed(3)}))`)
  if (dark > 0.01) layers.push(`linear-gradient(rgba(5,7,12,${dark.toFixed(3)}), rgba(5,7,12,${dark.toFixed(3)}))`)
  if (!layers.length) return null
  return <div style={{ position: 'absolute', inset: 0, background: layers.join(','), pointerEvents: 'none' }} />
}

/** 把一张瓦片地图以「透视倾斜地面 + 直立 billboard」的 2.5D 渲染（CSS 3D，浏览器做透视）。滚轮缩放。
 *  onIntent 给定时（游戏内）地图变成快捷操作面板：点物体/NPC/出口/地板 → 生成一句行动
 *  意图文本交给调用方（预填输入框，不自动发送——玩家保有最终否决权）。模组预览不传则纯展示。 */
export function MapView({ map, entities, assets, onIntent }: { map: TileMap; entities?: MapEntity[]; assets?: AssetLite[]; onIntent?: (text: string) => void }) {
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

  // ── 光源与迷雾 ─────────────────────────────────────────────
  // 玩家/队友是移动烛光；出入口是微弱琥珀光。没有瓦片级「已见过」数据，
  // 迷雾按「离最近光源的距离」压暗（有玩家在场才启用，模组预览不受影响）。
  const lights: { x: number; y: number; r: number; warm: number }[] = []
  for (const en of entities || []) {
    if (en.kind === 'player') lights.push({ x: en.x, y: en.y, r: 4.5, warm: 0.15 })
    else if (en.kind === 'ally') lights.push({ x: en.x, y: en.y, r: 3.5, warm: 0.10 })
  }
  const fog = lights.length > 0
  for (const e of map.entrances || []) lights.push({ x: e.x, y: e.y, r: 1.6, warm: 0.10 })

  const lightAt = (x: number, y: number) => {
    let bright = 0, warm = 0
    for (const L of lights) {
      const d = Math.hypot(x - L.x, y - L.y)
      bright = Math.max(bright, 1 - d / (L.r * 2.2))
      warm = Math.max(warm, L.warm * (1 - Math.min(1, d / L.r)))
    }
    const dark = fog ? Math.min(0.82, Math.max(0, (1 - bright) * 0.9 - 0.08)) : 0
    return { dark, warm }
  }

  // 固定视口 + 内部缩放/平移：滚轮缩放地图本身（容器不变大），可拖拽平移。
  const viewportRef = useRef<HTMLDivElement>(null)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  // 视角切换：2.5D 透视（默认，氛围）↔ 俯视 90°（信息密度高、不压缩）。偏好持久化。
  const [topdown, setTopdown] = useState(() => {
    try { return localStorage.getItem('map.topdown') === '1' } catch { return false }
  })
  const toggleView = () => {
    const next = !topdown
    try { localStorage.setItem('map.topdown', next ? '1' : '0') } catch { /* 忽略 */ }
    setTopdown(next)
  }
  const tilt = topdown ? 0 : TILT

  // 进入时按视口大小自适应缩放，并居中
  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const fit = Math.min(1, (el.clientWidth - 24) / sceneW, (el.clientHeight - 24) / sceneH)
    setZoom(fit > 0.1 ? fit : 1)
    setPan({ x: 0, y: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, topdown])

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

  // 拖拽平移与点击意图共存：mousedown 后累计位移超过阈值即视为拖拽，click 一律忽略。
  const movedRef = useRef(false)
  const onMouseDown = (e: React.MouseEvent) => {
    const start = { mx: e.clientX, my: e.clientY, px: pan.x, py: pan.y }
    movedRef.current = false
    setDragging(true)
    const move = (ev: MouseEvent) => {
      if (Math.hypot(ev.clientX - start.mx, ev.clientY - start.my) > 4) movedRef.current = true
      setPan({ x: start.px + (ev.clientX - start.mx), y: start.py + (ev.clientY - start.my) })
    }
    const up = () => { setDragging(false); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }
  const fireIntent = (text: string) => {
    if (onIntent && !movedRef.current) onIntent(text)
  }

  // 点空地板 → 「走到房间某侧」：按格坐标相对地图中心给出中文方位（屏幕上北下南）
  const directionOf = (x: number, y: number): string => {
    const nx = (x - (W - 1) / 2) / Math.max(W / 2, 1)
    const ny = (y - (H - 1) / 2) / Math.max(H / 2, 1)
    if (Math.abs(nx) < 0.25 && Math.abs(ny) < 0.25) return '中央'
    const ew = nx > 0.25 ? '东' : nx < -0.25 ? '西' : ''
    const ns = ny > 0.25 ? '南' : ny < -0.25 ? '北' : ''
    return `${ns}${ew}侧`
  }

  // 地面瓦片（含墙格的地面底，墙体另画直立板）
  const floors: React.ReactNode[] = []
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const c = at(x, y)
      if (c === ' ') continue
      // 地面：墙格底下铺 floor 素材；其余按字符类型取素材。非墙格可有逐格素材覆盖（tile_assets），否则按类型默认，再否则色块。
      const groundKind = c === '#' ? 'floor' : GLYPH_KIND[c]
      const ov = c !== '#' ? map.tile_assets?.[`${x},${y}`] : undefined
      const sprite = (ov && byId[ov]) || spriteFor(groundKind)
      const lt = lightAt(x, y)
      const doorGlow = c === '+' ? `radial-gradient(circle, rgba(${CANDLE},0.22) 0%, transparent 62%)` : undefined
      const bg = TILE_BG[c] || TILE_BG['.']
      const walkable = onIntent && c !== '#'
      floors.push(
        <div key={`f${x},${y}`}
          onClick={walkable ? () => fireIntent(`我走到房间${directionOf(x, y)}。`) : undefined}
          style={{
            position: 'absolute', left: x * TILE, top: y * TILE, width: TILE, height: TILE,
            background: sprite ? `center/100% 100% no-repeat url("${sprite}")` : bg[(x + y) % 2],
            imageRendering: 'pixelated',
            cursor: walkable ? 'pointer' : undefined,
          }}>
          {/* 俯视模式：墙直接画成平面块（顶面色），不再立墙板 */}
          {topdown && c === '#' && (
            <div style={{ position: 'absolute', inset: 0, background: sprite ? undefined : '#4a4230', boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.3), inset 0 1px 0 rgba(232,220,192,0.12)' }} />
          )}
          {!sprite && c === '+' && <div style={{ position: 'absolute', inset: '20% 28%', background: 'linear-gradient(#7a5427,#5c3d1c)', borderRadius: 2, boxShadow: `0 0 6px rgba(${CANDLE},0.35)` }} />}
          {!sprite && c === ':' && <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(rgba(232,220,192,0.10) 1px, transparent 1.5px)', backgroundSize: '9px 9px' }} />}
          {!sprite && c === '~' && <div style={{ position: 'absolute', inset: 0, background: 'repeating-linear-gradient(180deg, rgba(143,163,176,0.10) 0px, rgba(143,163,176,0.10) 1px, transparent 1px, transparent 7px)' }} />}
          {shade(lt.dark, lt.warm, doorGlow)}
        </div>,
      )
    }
  }

  // 烛光光晕：玩家/队友位置的平滑径向暖光（平铺在地面层之上、直立元素之下）
  const glows = lights.filter((L) => L.r > 2).map((L, i) => (
    <div key={`glow${i}`} style={{
      position: 'absolute',
      left: (L.x + 0.5) * TILE - L.r * TILE, top: (L.y + 0.5) * TILE - L.r * TILE,
      width: L.r * 2 * TILE, height: L.r * 2 * TILE, borderRadius: '50%',
      background: `radial-gradient(circle, rgba(${CANDLE},0.18) 0%, rgba(${CANDLE},0.07) 45%, transparent 70%)`,
      pointerEvents: 'none',
    }} />
  ))

  // 墙顶面几何：直立墙高 WALL_H 的顶端，在倾斜地面坐标里等价于「上移 cos、抬 Z sin」
  const tiltRad = TILT * Math.PI / 180
  const topDy = WALL_H * Math.cos(tiltRad)
  const topDz = WALL_H * Math.sin(tiltRad)

  // 直立元素：墙板 + token，自上而下排序（远先画、近压上）。俯视模式不立墙板（墙在地面层画平面块）。
  const stand: { y: number; key: string; el: React.ReactNode }[] = []
  for (let y = 0; y < H && !topdown; y++) {
    for (let x = 0; x < W; x++) {
      if (at(x, y) !== '#') continue
      const wov = map.tile_assets?.[`${x},${y}`]
      const ws = (wov && byId[wov]) || spriteFor('wall')
      const lt = lightAt(x, y)
      // 水平中线以下（靠近镜头）的墙半透明：近处的墙会挡住内部，远处（上半）的墙不挡、保持不透明
      const isNear = y * 2 >= H
      stand.push({ y, key: `w${x},${y}`, el: (
        <Fragment>
          <Billboard x={x} y={y} h={WALL_H}>
            <div style={{ width: TILE, height: WALL_H, imageRendering: 'pixelated', opacity: isNear ? 0.4 : 1, position: 'relative',
              background: ws ? `center/100% 100% no-repeat url("${ws}")` : 'linear-gradient(#524a35 0%, #453d2b 55%, #322b1c 100%)',
              boxShadow: ws ? undefined : `inset 0 1px 0 rgba(${CANDLE},0.22), inset 0 -1px 0 rgba(0,0,0,0.4), 0 2px 4px rgba(0,0,0,0.45)` }}>
              {shade(lt.dark, lt.warm * 0.6)}
            </div>
          </Billboard>
          {/* 顶面：伪厚度——色块墙给一片贴地的「墙顶」，接在直立面顶边上 */}
          {!ws && (
            <div style={{
              position: 'absolute', left: x * TILE, top: y * TILE, width: TILE, height: TILE / 2,
              transform: `translate3d(0, ${-topDy}px, ${topDz}px)`,
              background: '#4a4230',
              boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.25), inset 0 1px 0 rgba(232,220,192,0.12)',
              opacity: isNear ? 0.4 : 1, pointerEvents: 'none',
            }}>
              {shade(lt.dark, lt.warm * 0.6)}
            </div>
          )}
        </Fragment>
      ) })
    }
  }

  // ── token ──────────────────────────────────────────────────
  // 有素材 → 素材精灵；人物（玩家/队友/NPC/敌人）→ 色环+名字首字；物件/出口 → 暗盘+图标。
  // 可读性三件套：① 常显微标签（缩放太小隐藏防糊）；② 撞脸兜底——同一素材被 ≥2 个
  // 人物引用时全部回退「首字+色环」（杜绝俩 NPC 一张脸）；③ 同格 token 扇形错开。
  const anyActive = (entities || []).some((e) => e.active)
  const showLabels = zoom >= 0.55

  // ② 撞脸检测：数一遍人物 token 的素材 URL 引用数
  const personSpriteUses = new Map<string, number>()
  const countPerson = (kind?: string, assetId?: string) => {
    const s = spriteFor(kind, assetId)
    if (s) personSpriteUses.set(s, (personSpriteUses.get(s) || 0) + 1)
  }
  for (const n of map.npc_pos || []) countPerson(n.hostile ? 'enemy' : 'npc', n.asset_id)
  for (const en of entities || []) if (en.kind !== 'item') countPerson(en.kind, en.asset_id)
  const dupSprite = (kind?: string, assetId?: string) => {
    const s = spriteFor(kind, assetId)
    return !!s && (personSpriteUses.get(s) || 0) >= 2
  }

  // ③ 同格错开：先数每格 token 数，再按序分配水平偏移
  const occTotal = new Map<string, number>()
  const occSeen = new Map<string, number>()
  const bump = (x: number, y: number) => occTotal.set(`${x},${y}`, (occTotal.get(`${x},${y}`) || 0) + 1)
  for (const o of map.objects || []) bump(o.x, o.y)
  for (const e of map.entrances || []) bump(e.x, e.y)
  for (const n of map.npc_pos || []) bump(n.x, n.y)
  for (const en of entities || []) bump(en.x, en.y)
  const fanAt = (x: number, y: number): { dx: number; idx: number } => {
    const k = `${x},${y}`
    const n = occTotal.get(k) || 1
    if (n <= 1) return { dx: 0, idx: 0 }
    const i = occSeen.get(k) || 0
    occSeen.set(k, i + 1)
    return { dx: (i - (n - 1) / 2) * 13, idx: i }
  }

  // ① 微标签：billboard 内贴地站立（在 token 脚下）。注意不能放到锚点（底边）之下——
  // 那会转到地平面以下，被 preserve-3d 深度排序裁掉、只剩一条压扁的痕。
  // 同格多 token 时按 idx 竖向叠放，避免标签互相压字。
  const tokLabel = (text: string, idx = 0) => showLabels ? (
    <div style={{
      position: 'absolute', bottom: -2 - idx * 10, left: '50%', transform: 'translateX(-50%)',
      fontSize: 9, lineHeight: 1.15, color: PARCH, whiteSpace: 'nowrap', maxWidth: 76,
      overflow: 'hidden', textOverflow: 'ellipsis', pointerEvents: 'none',
      textShadow: '0 1px 2px rgba(0,0,0,0.95), 0 0 4px rgba(0,0,0,0.9)',
    }}>{text}</div>
  ) : null

  const spriteEl = (label: string, sprite: string, idx = 0, intent?: string) => (
    <div title={label}
      onClick={intent ? () => fireIntent(intent) : undefined}
      style={{ width: TILE, height: TILE, imageRendering: 'pixelated', pointerEvents: 'auto', cursor: intent ? 'pointer' : undefined, filter: 'drop-shadow(0 2px 2px rgba(0,0,0,0.5))', background: `center bottom/contain no-repeat url("${sprite}")`, position: 'relative' }}>
      {tokLabel(label, idx)}
    </div>
  )
  const stem = <div style={{ width: 2, height: 7, background: 'rgba(0,0,0,0.45)' }} />
  const charTok = (x: number, y: number, label: string, ring: string, key: string, kind?: string, assetId?: string, glow?: boolean, intent?: string) => {
    // 撞脸兜底：素材重复即回退首字+色环；色环底色掺一点名字私有色相辅助区分
    const sprite = dupSprite(kind, assetId) ? undefined : spriteFor(kind, assetId)
    const initial = (label || '?').trim().charAt(0) || '?'
    const { dx, idx } = fanAt(x, y)
    stand.push({
      y, key, el: (
        <Billboard x={x} y={y} h={sprite ? TILE : TOKEN_H} z={2} dx={dx} tilt={tilt}>
          {sprite ? spriteEl(label, sprite, idx, intent) : (
            <div title={label}
              onClick={intent ? () => fireIntent(intent) : undefined}
              style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', pointerEvents: 'auto', cursor: intent ? 'pointer' : undefined, position: 'relative' }}>
              {glow && <div style={{ position: 'absolute', left: '50%', top: 1, width: 26, height: 26, marginLeft: -13, borderRadius: '50%', boxShadow: `0 0 10px 5px rgba(${CANDLE},0.5)`, animation: 'mapTokenPulse 2.6s ease-in-out infinite', pointerEvents: 'none' }} />}
              <div style={{ width: 26, height: 26, borderRadius: '50%', border: `2px solid ${ring}`, background: `linear-gradient(rgba(10,12,17,0.88), rgba(10,12,17,0.88)), hsl(${nameHue(label)},40%,42%)`, color: PARCH, fontSize: 11, fontWeight: 700, lineHeight: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 4px rgba(0,0,0,0.6)', position: 'relative' }}>{initial}</div>
              {stem}
              {tokLabel(label, idx)}
            </div>
          )}
        </Billboard>
      ),
    })
  }
  const iconTok = (x: number, y: number, label: string, color: string, Icon: IconT, key: string, kind?: string, assetId?: string, intent?: string) => {
    const sprite = spriteFor(kind, assetId)
    const { dx, idx } = fanAt(x, y)
    stand.push({
      y, key, el: (
        <Billboard x={x} y={y} h={sprite ? TILE : TOKEN_H} z={2} dx={dx} tilt={tilt}>
          {sprite ? spriteEl(label, sprite, idx, intent) : (
            <div title={label}
              onClick={intent ? () => fireIntent(intent) : undefined}
              style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', pointerEvents: 'auto', cursor: intent ? 'pointer' : undefined, position: 'relative' }}>
              <div style={{ width: 24, height: 24, borderRadius: '50%', background: 'rgba(10,12,17,0.9)', border: `1.5px solid ${color}`, display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 4px rgba(0,0,0,0.6)' }}>
                <Icon size={13} color={color} />
              </div>
              {stem}
              {tokLabel(label, idx)}
            </div>
          )}
        </Billboard>
      ),
    })
  }
  // 点击意图（onIntent 给定时）：物体→调查、NPC→走近、出口→走向；玩家/队友不生成意图
  const it = (text: string) => (onIntent ? text : undefined)
  for (const o of map.objects || []) {
    const isItem = o.kind === 'item'
    const color = isItem ? '#c9973b' : o.kind === 'feature' ? '#8fa3b0' : '#8a7a5c'
    iconTok(o.x, o.y, o.name, color, objIcon(o.name, o.kind), `o${o.x},${o.y},${o.name}`, o.kind || 'furniture', o.asset_id, it(`我调查【${o.name}】。`))
  }
  for (const e of map.entrances || []) iconTok(e.x, e.y, e.name, '#d4a24e', DoorOpen, `e${e.x},${e.y},${e.name}`, 'door', e.asset_id, it(`我走向「${e.name}」。`))
  for (const n of map.npc_pos || []) n.hostile
    ? charTok(n.x, n.y, n.name, '#a13a42', `n${n.x},${n.y},${n.name}`, 'enemy', n.asset_id, false, it(`我走近${n.name}。`))
    : charTok(n.x, n.y, n.name, '#6e7f8d', `n${n.x},${n.y},${n.name}`, 'npc', n.asset_id, false, it(`我走近${n.name}。`))
  for (const en of entities || []) {
    if (en.kind === 'item') { iconTok(en.x, en.y, en.name, '#c9973b', Box, `en${en.x},${en.y},${en.name}`, en.kind, en.asset_id, it(`我调查【${en.name}】。`)); continue }
    const ring = { player: '#d4a24e', ally: '#8fa3b0', npc: '#6e7f8d', enemy: '#a13a42' }[en.kind]
    const glow = anyActive ? !!en.active : en.kind === 'player'
    const intent = en.kind === 'npc' || en.kind === 'enemy' ? it(`我走近${en.name}。`) : undefined
    charTok(en.x, en.y, en.name, ring, `en${en.x},${en.y},${en.name}`, en.kind, en.asset_id, glow, intent)
  }
  stand.sort((a, b) => a.y - b.y)

  // 场景盒：给透视投影留足横向（近边变宽）与纵向（墙体抬高）余量。
  const sceneW = planeW * (topdown ? 1 : 1.35) + 40
  const sceneH = topdown ? planeH + TOKEN_H + 40 : planeH * 0.55 + WALL_H + 60

  return (
    <div
      ref={viewportRef}
      onMouseDown={onMouseDown}
      style={{
        position: 'relative', width: '100%', height: VIEWPORT_H, overflow: 'hidden',
        cursor: dragging ? 'grabbing' : 'grab', userSelect: 'none', touchAction: 'none',
        borderRadius: 6,
        // 地图自带深夜基底：不依赖页面主题，烛光/暗角在任何外壳下都成立
        background: 'radial-gradient(ellipse at 50% 42%, #14161d 0%, #0c0e13 70%)',
        boxShadow: 'inset 0 0 0 1px rgba(212,162,78,0.14)',
      }}
    >
      {/* 呼吸光圈动画：纯 CSS 合成器动画，无 JS 重绘循环 */}
      <style>{'@keyframes mapTokenPulse{0%,100%{opacity:.35;transform:scale(.85)}50%{opacity:.9;transform:scale(1.12)}}'}</style>
      <button onMouseDown={(e) => e.stopPropagation()} onClick={() => setZoom((z) => Math.max(0.2, z * 0.89))} title="缩小" style={zoomBtn(8)}><ZoomOut size={14} /></button>
      <button onMouseDown={(e) => e.stopPropagation()} onClick={() => setZoom((z) => Math.min(4, z * 1.12))} title="放大" style={zoomBtn(40)}><ZoomIn size={14} /></button>
      <button onMouseDown={(e) => e.stopPropagation()} onClick={toggleView}
        title={topdown ? '切换到 2.5D 透视（氛围）' : '切换到俯视（信息密度高）'}
        style={{ ...zoomBtn(72), fontSize: 10, fontWeight: 700 }}>
        {topdown ? '3D' : '2D'}
      </button>
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
            transformStyle: 'preserve-3d', transform: `rotateX(${tilt}deg)`,
          }}>
            {floors}
            {glows}
            {stand.map((s) => <Fragment key={s.key}>{s.el}</Fragment>)}
          </div>
        </div>
      </div>
      {/* 暗角：烛光探索图的边缘渐暗 */}
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 3, background: 'radial-gradient(ellipse at 50% 45%, transparent 52%, rgba(4,6,10,0.55) 100%)' }} />
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
