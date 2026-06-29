import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getApiBase, getPlayerToken, mediaUrl } from '../api/client'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiReturnArrow } from 'react-icons/gi'
import { Upload, Trash2, ImageIcon } from 'lucide-react'

interface Asset { id: string; name: string; kind: string; tags: string[]; image_url: string; builtin: boolean }

const KINDS: { value: string; label: string }[] = [
  { value: 'floor', label: '地板' },
  { value: 'wall', label: '墙' },
  { value: 'door', label: '门' },
  { value: 'water', label: '水' },
  { value: 'rubble', label: '碎石' },
  { value: 'furniture', label: '家具' },
  { value: 'item', label: '物品' },
  { value: 'npc', label: 'NPC' },
  { value: 'enemy', label: '敌人' },
  { value: 'player', label: '玩家' },
  { value: 'feature', label: '景物' },
]
const kindLabel = (k: string) => KINDS.find((o) => o.value === k)?.label || k

export function AssetsPage() {
  const navigate = useNavigate()
  const fileRef = useRef<HTMLInputElement>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [kind, setKind] = useState('furniture')
  const [tags, setTags] = useState('')
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [filter, setFilter] = useState('all')

  const fetchAssets = useCallback(async () => {
    try { setAssets(await api.get<Asset[]>('/assets')) } catch { /* 静默 */ }
  }, [])
  useEffect(() => { fetchAssets() }, [fetchAssets])

  const pickFile = (f: File | undefined) => {
    if (!f) return
    if (!f.type.startsWith('image/')) { toast.error('请选择图片（png/jpg/webp/gif）'); return }
    setFile(f)
    if (!name.trim()) setName(f.name.replace(/\.[^.]+$/, ''))
  }

  const handleUpload = async () => {
    if (!file) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('name', name.trim() || file.name)
      form.append('kind', kind)
      form.append('tags', tags)
      const res = await fetch(`${getApiBase()}/assets`, {
        method: 'POST', headers: { 'X-Player-Token': getPlayerToken() }, body: form,
      })
      if (!res.ok) throw new Error(await res.text())
      setFile(null); setName(''); setTags('')
      if (fileRef.current) fileRef.current.value = ''
      toast.success('素材已上传')
      fetchAssets()
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally { setUploading(false) }
  }

  const remove = async (id: string) => {
    try { await api.delete(`/assets/${id}`); fetchAssets(); toast.success('素材已删除') }
    catch (e) { toast.error(e instanceof Error ? e.message : '删除失败') }
  }

  const shown = filter === 'all' ? assets : assets.filter((a) => a.kind === filter)
  // 渲染器以「每类最新上传」为默认素材；这里据同序（列表为 created_at 倒序）标出默认。
  const defaultIdByKind: Record<string, string> = {}
  for (const a of assets) if (!(a.kind in defaultIdByKind)) defaultIdByKind[a.kind] = a.id

  return (
    <div className="max-w-4xl">
      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm"><GiReturnArrow /> 返回</button>
        <h2 className="page-title !mb-0">素材库</h2>
      </div>
      <p className="text-sm mb-4" style={{ color: 'var(--color-text-secondary)' }}>
        上传地图素材（独立 PNG/图片）。地图按「类型默认素材 / 指定素材」引用渲染——加素材只需在此上传，无需改任何代码。每类的默认素材取该类最新上传的一张。
      </p>

      {/* 上传 */}
      <div className="card mb-6">
        <h3 className="card-title flex items-center gap-2"><Upload size={16} /> 上传素材</h3>
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); pickFile(e.dataTransfer.files[0]) }}
          onClick={() => fileRef.current?.click()}
          className="border-2 border-dashed rounded-md p-5 mb-3 text-center cursor-pointer transition-colors"
          style={{ borderColor: dragOver ? 'var(--color-accent)' : 'var(--color-border)', background: dragOver ? 'rgba(139,37,0,0.04)' : 'rgba(255,255,255,0.2)' }}
        >
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = '' }} />
          {file ? (
            <div className="flex items-center justify-center gap-3">
              <img src={URL.createObjectURL(file)} alt="" style={{ width: 48, height: 48, objectFit: 'contain', imageRendering: 'pixelated' }} />
              <div className="text-left">
                <p className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>{file.name}</p>
                <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>{(file.size / 1024).toFixed(0)} KB · 点击可重选</p>
              </div>
            </div>
          ) : (
            <div>
              <ImageIcon className="mx-auto mb-2" size={24} style={{ color: 'var(--color-text-secondary)' }} />
              <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>拖拽图片到此处，或点击选择（单张 ≤ 4MB）</p>
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称" className="px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', width: 160 }} />
          <Select value={kind} onValueChange={setKind}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>{KINDS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
          </Select>
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="标签（逗号分隔）" className="px-2 py-1 rounded text-sm flex-1 min-w-[140px]" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
          <button onClick={handleUpload} disabled={uploading || !file} className="btn-primary">{uploading ? '上传中…' : '上传'}</button>
        </div>
      </div>

      {/* 列表 */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>筛选</span>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            {KINDS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
          </SelectContent>
        </Select>
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>共 {shown.length} 件</span>
      </div>

      {shown.length === 0 ? (
        <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>暂无素材，请上传。</p>
      ) : (
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))' }}>
          {shown.map((a) => (
            <div key={a.id} className="card !p-2 relative">
              <div className="rounded mb-1 flex items-center justify-center" style={{ height: 80, background: 'repeating-conic-gradient(#0001 0% 25%, transparent 0% 50%) 50%/16px 16px, var(--color-bg-tertiary)' }}>
                <img src={mediaUrl(a.image_url)} alt={a.name} style={{ maxWidth: '90%', maxHeight: 72, imageRendering: 'pixelated' }} />
              </div>
              <div className="flex items-center justify-between gap-1">
                <span className="text-xs truncate" title={a.name}>{a.name}</span>
                {!a.builtin && (
                  <ConfirmDialog title="删除素材" description={`确定删除「${a.name}」？`} confirmLabel="删除" onConfirm={() => remove(a.id)}>
                    {(open) => <button onClick={open} className="p-0.5 flex-shrink-0" style={{ color: 'var(--color-danger)' }} title="删除"><Trash2 size={12} /></button>}
                  </ConfirmDialog>
                )}
              </div>
              <div className="flex items-center gap-1 mt-0.5 flex-wrap">
                <span className="badge !text-[10px]">{kindLabel(a.kind)}</span>
                {defaultIdByKind[a.kind] === a.id && <span className="badge !text-[10px]" style={{ color: 'var(--color-success)', borderColor: 'var(--color-success)' }}>默认</span>}
              </div>
              {a.tags.length > 0 && <div className="text-[10px] mt-0.5 truncate" style={{ color: 'var(--color-text-secondary)' }}>{a.tags.join('、')}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
