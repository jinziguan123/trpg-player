import { useEffect, useRef, useState } from 'react'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Save, X } from 'lucide-react'
import type { TileMap, AssetLite } from './MapView'

const CELL = 26
// 地形画笔（glyph + 标签 + 占位色）
const TERRAIN = [
  { g: '.', label: '地板', color: '#cdb487' },
  { g: '#', label: '墙', color: '#7d6c52' },
  { g: '+', label: '门', color: '#b5824a' },
  { g: '~', label: '水', color: '#5f86a6' },
  { g: ':', label: '碎石', color: '#bfa97f' },
  { g: ' ', label: '空', color: 'transparent' },
]
const TERRAIN_COLOR: Record<string, string> = Object.fromEntries(TERRAIN.map((t) => [t.g, t.color]))
// 物体类型（放置层）；door→entrances、npc→npc_pos、其余→objects
const OBJ_KINDS = [
  { value: 'furniture', label: '家具' },
  { value: 'item', label: '物品' },
  { value: 'npc', label: 'NPC' },
  { value: 'enemy', label: '敌人' },
  { value: 'feature', label: '景物' },
  { value: 'door', label: '门(出口)' },
]
const KIND_COLOR: Record<string, string> = { furniture: '#7a6248', item: '#b8860b', npc: '#3b6ea5', enemy: '#a33', feature: '#6a5', door: '#2d7d46' }

type Tok = { name: string; x: number; y: number; kind?: string; asset_id?: string; to?: string; hostile?: boolean }
const glyphKind = (g: string): string => ({ '#': 'wall', '.': 'floor', '+': 'door', '~': 'water', ':': 'rubble' }[g] || '')

function defaultTiles(w: number, h: number): string[] {
  return Array.from({ length: h }, (_, y) =>
    Array.from({ length: w }, (_, x) => (y === 0 || y === h - 1 || x === 0 || x === w - 1 ? '#' : '.')).join(''))
}

/** 俯视网格地图编辑器：刷地形（点/拖）、放置/移除物体（点选切换）、改尺寸、存回 scene.map。 */
export function MapEditor({ initial, assets, onSave, onCancel, onAIGenerate, onImageGenerate, title }: {
  initial?: TileMap
  assets: AssetLite[]
  onSave: (m: TileMap) => void
  onCancel: () => void
  onAIGenerate?: (hint: string) => Promise<TileMap | null>
  onImageGenerate?: (file: File) => Promise<TileMap | null>
  title?: string
}) {
  const [w, setW] = useState(initial?.w || 14)
  const [h, setH] = useState(initial?.h || 10)
  const [tiles, setTiles] = useState<string[]>(initial?.tiles?.length ? [...initial.tiles] : defaultTiles(initial?.w || 14, initial?.h || 10))
  const [objects, setObjects] = useState<Tok[]>(() => [...(initial?.objects || []), ...(initial?.npc_pos || []).map((n) => ({ ...n, kind: n.hostile ? 'enemy' : 'npc' })), ...(initial?.entrances || []).map((e) => ({ ...e, kind: 'door' }))])
  const [notes, setNotes] = useState(initial?.notes || '')
  const [tileAssets, setTileAssets] = useState<Record<string, string>>(initial?.tile_assets || {})
  const [terrainAsset, setTerrainAsset] = useState('')   // 当前地形画笔选用的具体素材（空=该类默认）

  const [tool, setTool] = useState('#')      // 当前画笔：地形 glyph 或 'obj'
  const [objKind, setObjKind] = useState('furniture')
  const [objName, setObjName] = useState('')
  const [objAsset, setObjAsset] = useState('')
  const [painting, setPainting] = useState(false)
  const [aiHint, setAiHint] = useState('')
  const [aiBusy, setAiBusy] = useState(false)

  const applyMap = (m: TileMap) => {
    setW(m.w); setH(m.h); setTiles([...m.tiles])
    setObjects([...(m.objects || []), ...(m.npc_pos || []).map((n) => ({ ...n, kind: n.hostile ? 'enemy' : 'npc' })), ...(m.entrances || []).map((e) => ({ ...e, kind: 'door' }))])
    setTileAssets(m.tile_assets || {})
    setNotes(m.notes || '')
  }
  const runAI = async () => {
    if (!onAIGenerate) return
    setAiBusy(true)
    try { const m = await onAIGenerate(aiHint); if (m) applyMap(m) } finally { setAiBusy(false) }
  }
  const imgRef = useRef<HTMLInputElement>(null)
  const runImage = async (file?: File) => {
    if (!onImageGenerate || !file) return
    setAiBusy(true)
    try { const m = await onImageGenerate(file); if (m) applyMap(m) } finally { setAiBusy(false) }
  }

  useEffect(() => {
    const up = () => setPainting(false)
    window.addEventListener('mouseup', up)
    return () => window.removeEventListener('mouseup', up)
  }, [])

  const byKind: Record<string, string> = {}, byId: Record<string, string> = {}
  for (const a of assets) { if (!(a.kind in byKind)) byKind[a.kind] = a.image_url; byId[a.id] = a.image_url }
  const at = (x: number, y: number) => (tiles[y] && tiles[y][x]) || ' '
  const setTile = (x: number, y: number, g: string) =>
    setTiles((t) => t.map((row, ry) => (ry === y ? row.slice(0, x) + g + row.slice(x + 1) : row)))

  const resize = (nw: number, nh: number) => {
    nw = Math.max(4, Math.min(40, nw)); nh = Math.max(4, Math.min(40, nh))
    setW(nw); setH(nh)
    setTiles((old) => Array.from({ length: nh }, (_, y) =>
      Array.from({ length: nw }, (_, x) => (old[y] && old[y][x]) || '.').join('')))
    setTileAssets((ta) => Object.fromEntries(Object.entries(ta).filter(([k]) => { const [x, y] = k.split(',').map(Number); return x < nw && y < nh })))
  }

  // 刷地形：写字符 + 记录/清除该格的素材覆盖（terrainAsset 为空=用该类默认）
  const paintTerrain = (x: number, y: number) => {
    setTile(x, y, tool)
    const key = `${x},${y}`
    setTileAssets((ta) => {
      const n = { ...ta }
      if (terrainAsset && glyphKind(tool)) n[key] = terrainAsset
      else delete n[key]
      return n
    })
  }

  const toggleToken = (x: number, y: number) => {
    const i = objects.findIndex((o) => o.x === x && o.y === y)
    if (i >= 0) { setObjects((o) => o.filter((_, j) => j !== i)); return }
    const t: Tok = { name: objName.trim() || (OBJ_KINDS.find((k) => k.value === objKind)?.label ?? '物体'), x, y, kind: objKind }
    if (objAsset) t.asset_id = objAsset
    if (objKind === 'door') t.to = ''
    setObjects((o) => [...o, t])
  }

  const onCell = (x: number, y: number) => { tool === 'obj' ? toggleToken(x, y) : paintTerrain(x, y) }

  const save = () => {
    const aid = (o: Tok) => (o.asset_id ? { asset_id: o.asset_id } : {})
    // 关键：npc_pos / entrances 也要带上 asset_id（之前漏了 → 不同 token 存成同一默认贴图）
    const npc_pos = objects.filter((o) => o.kind === 'npc' || o.kind === 'enemy').map((o) => ({ name: o.name, x: o.x, y: o.y, hostile: o.kind === 'enemy', ...aid(o) }))
    const entrances = objects.filter((o) => o.kind === 'door').map((o) => ({ name: o.name, x: o.x, y: o.y, to: o.to || '', ...aid(o) }))
    const objs = objects.filter((o) => o.kind !== 'npc' && o.kind !== 'enemy' && o.kind !== 'door').map((o) => ({ name: o.name, x: o.x, y: o.y, kind: o.kind, ...aid(o) }))
    onSave({ w, h, tiles, objects: objs, entrances, npc_pos, tile_assets: tileAssets, notes })
  }

  const assetsOfKind = assets.filter((a) => a.kind === objKind)

  return (
    <div>
      {title && <div className="text-sm font-semibold mb-2" style={{ color: 'var(--color-text-accent)' }}>{title}</div>}
      {/* AI 生成（变体地图据基础图改） */}
      {onAIGenerate && (
        <div className="flex items-center gap-2 mb-2 p-2 rounded flex-wrap" style={{ background: 'var(--color-bg-tertiary)' }}>
          <input value={aiHint} onChange={(e) => setAiHint(e.target.value)} placeholder="描述变化，如「西墙被打破，露出北侧密室」「管家移动到门口」" className="px-2 py-1 rounded text-sm flex-1 min-w-[200px]" style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)' }} />
          <button onClick={runAI} disabled={aiBusy} className="btn-secondary text-sm" style={aiBusy ? { opacity: 0.6 } : undefined}>{aiBusy ? 'AI 生成中…' : 'AI 据基础图生成'}</button>
        </div>
      )}
      {/* 多模态：据模组自带地图图片生成（需视觉 LLM） */}
      {onImageGenerate && (
        <div className="flex items-center gap-2 mb-2 p-2 rounded flex-wrap" style={{ background: 'var(--color-bg-tertiary)' }}>
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>有模组自带地图图片？</span>
          <input ref={imgRef} type="file" accept="image/*" className="hidden" onChange={(e) => { runImage(e.target.files?.[0]); e.target.value = '' }} />
          <button onClick={() => imgRef.current?.click()} disabled={aiBusy} className="btn-secondary text-sm" style={aiBusy ? { opacity: 0.6 } : undefined}>{aiBusy ? '识别中…' : '上传地图图片识别生成'}</button>
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>视觉模型据图转成瓦片地图（需在设置选支持视觉的模型）</span>
        </div>
      )}
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>地形</span>
        {TERRAIN.map((t) => (
          <button key={t.g} onClick={() => { setTool(t.g); setTerrainAsset('') }} title={t.label}
            className="text-xs px-2 py-1 rounded" style={tool === t.g ? { background: 'var(--color-accent)', color: '#fff' } : { background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>{t.label}</button>
        ))}
        <button onClick={() => setTool('obj')} className="text-xs px-2 py-1 rounded" style={tool === 'obj' ? { background: 'var(--color-accent)', color: '#fff' } : { background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>放置物体</button>
        <span className="ml-auto text-xs" style={{ color: 'var(--color-text-secondary)' }}>尺寸</span>
        <input type="number" value={w} onChange={(e) => resize(Number(e.target.value), h)} className="w-14 px-1 py-0.5 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
        <span style={{ color: 'var(--color-text-secondary)' }}>×</span>
        <input type="number" value={h} onChange={(e) => resize(w, Number(e.target.value))} className="w-14 px-1 py-0.5 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
      </div>

      {/* 地形画笔的素材选择（地板/墙/门… 可选具体素材，否则用该类默认） */}
      {tool !== 'obj' && glyphKind(tool) && (
        <div className="flex items-center gap-2 mb-2 p-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{TERRAIN.find((t) => t.g === tool)?.label} 素材</span>
          <Select value={terrainAsset || 'default'} onValueChange={(v) => setTerrainAsset(v === 'default' ? '' : v)}>
            <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="default">默认素材</SelectItem>
              {assets.filter((a) => a.kind === glyphKind(tool)).map((a) => <SelectItem key={a.id} value={a.id}>{a.name || a.id.slice(0, 6)}</SelectItem>)}
            </SelectContent>
          </Select>
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.8 }}>选定后刷的格用该素材；选「默认」刷可清除覆盖</span>
        </div>
      )}
      {/* 放置物体的参数 */}
      {tool === 'obj' && (
        <div className="flex flex-wrap items-center gap-2 mb-2 p-2 rounded" style={{ background: 'var(--color-bg-tertiary)' }}>
          <Select value={objKind} onValueChange={(v) => { setObjKind(v); setObjAsset('') }}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>{OBJ_KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}</SelectContent>
          </Select>
          <input value={objName} onChange={(e) => setObjName(e.target.value)} placeholder="名称（如 石棺）" className="px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-card)', border: '1px solid var(--color-border)', width: 140 }} />
          <Select value={objAsset || 'default'} onValueChange={(v) => setObjAsset(v === 'default' ? '' : v)}>
            <SelectTrigger className="w-32"><SelectValue placeholder="素材" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="default">默认素材</SelectItem>
              {assetsOfKind.map((a) => <SelectItem key={a.id} value={a.id}>{a.name || a.id.slice(0, 6)}</SelectItem>)}
            </SelectContent>
          </Select>
          <span className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.8 }}>点格放置，点已有物体可移除</span>
        </div>
      )}

      {/* 俯视网格 */}
      <div className="overflow-auto rounded p-2 mb-2" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', maxHeight: 420 }}>
        <div style={{ position: 'relative', width: w * CELL, height: h * CELL, userSelect: 'none' }}
          onMouseDown={() => setPainting(true)}>
          {tiles.map((row, y) => Array.from({ length: w }, (_, x) => {
            const g = at(x, y)
            const ov = tileAssets[`${x},${y}`]
            const sprite = (ov && byId[ov]) || byKind[glyphKind(g)]
            return (
              <div key={`${x},${y}`}
                onMouseDown={() => onCell(x, y)}
                onMouseEnter={() => { if (painting && tool !== 'obj') paintTerrain(x, y) }}
                style={{
                  position: 'absolute', left: x * CELL, top: y * CELL, width: CELL, height: CELL, cursor: 'pointer',
                  background: sprite ? `center/100% 100% no-repeat url("${sprite}")` : (TERRAIN_COLOR[g] ?? '#cdb487'),
                  boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.08)', imageRendering: 'pixelated',
                }} />
            )
          }))}
          {objects.map((o, i) => (
            <div key={i} title={`${o.name}（点移除）`}
              onMouseDown={(e) => { e.stopPropagation(); setObjects((arr) => arr.filter((_, j) => j !== i)) }}
              style={{ position: 'absolute', left: o.x * CELL + 3, top: o.y * CELL + 3, width: CELL - 6, height: CELL - 6, borderRadius: '50%', cursor: 'pointer', background: byId[o.asset_id || ''] ? `center/contain no-repeat url("${byId[o.asset_id!]}")` : (KIND_COLOR[o.kind || 'furniture'] || '#7a6248'), boxShadow: '0 0 0 1px #0006' }} />
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="布局说明（可选）" className="flex-1 px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
        <button onClick={onCancel} className="btn-secondary flex items-center gap-1 text-sm"><X size={14} /> 取消</button>
        <button onClick={save} className="btn-primary flex items-center gap-1 text-sm"><Save size={14} /> 保存地图</button>
      </div>
    </div>
  )
}
