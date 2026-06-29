import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getApiBase, getPlayerToken, mediaUrl } from '../api/client'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiReturnArrow } from 'react-icons/gi'
import { Upload, Trash2, ImageIcon, X, Pencil, Plus, Tags } from 'lucide-react'
import { useAssetCategories } from '../components/module/useAssetCategories'

interface Asset { id: string; name: string; kind: string; tags: string[]; image_url: string; builtin: boolean; is_default: boolean; source?: string; license?: string }

export function AssetsPage() {
  const navigate = useNavigate()
  const fileRef = useRef<HTMLInputElement>(null)
  const [cats, reloadCats] = useAssetCategories()
  const kindLabel = (k: string) => cats.find((c) => c.key === k)?.label || k
  const [assets, setAssets] = useState<Asset[]>([])
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [kind, setKind] = useState('furniture')
  const [tags, setTags] = useState('')
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [filter, setFilter] = useState('all')
  const [detail, setDetail] = useState<Asset | null>(null)
  const [showCats, setShowCats] = useState(false)
  const [newCatKey, setNewCatKey] = useState('')
  const [newCatLabel, setNewCatLabel] = useState('')
  // 详情编辑态
  const [editName, setEditName] = useState('')
  const [editKind, setEditKind] = useState('')

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
    try { await api.delete(`/assets/${id}`); setDetail(null); fetchAssets(); toast.success('素材已删除') }
    catch (e) { toast.error(e instanceof Error ? e.message : '删除失败') }
  }

  const setDefault = async (a: Asset) => {
    try {
      const updated = await api.post<Asset>(`/assets/${a.id}/default`)
      setDetail((d) => (d && d.id === a.id ? { ...d, is_default: true } : d))
      void updated; fetchAssets(); toast.success(`已设为「${kindLabel(a.kind)}」默认素材`)
    } catch { toast.error('设置失败') }
  }

  const openDetail = (a: Asset) => { setDetail(a); setEditName(a.name); setEditKind(a.kind) }
  const saveEdit = async () => {
    if (!detail) return
    try {
      const updated = await api.patch<Asset>(`/assets/${detail.id}`, { name: editName, kind: editKind })
      setDetail(updated); fetchAssets(); toast.success('已保存')
    } catch (e) { toast.error(e instanceof Error ? e.message : '保存失败') }
  }

  const addCategory = async () => {
    const key = newCatKey.trim()
    if (!key) return
    try { await api.post('/asset-categories', { key, label: newCatLabel.trim() || key }); setNewCatKey(''); setNewCatLabel(''); reloadCats(); toast.success('类别已新增') }
    catch (e) { toast.error(e instanceof Error ? e.message : '新增失败') }
  }
  const renameCategory = async (key: string, label: string) => {
    try { await api.put(`/asset-categories/${key}`, { label }); reloadCats() }
    catch (e) { toast.error(e instanceof Error ? e.message : '改名失败') }
  }
  const deleteCategory = async (key: string) => {
    try { await api.delete(`/asset-categories/${key}`); reloadCats(); toast.success('类别已删除') }
    catch (e) { toast.error(e instanceof Error ? e.message : '删除失败') }
  }

  const shown = filter === 'all' ? assets : assets.filter((a) => a.kind === filter)

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
            <SelectContent>{cats.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}</SelectContent>
          </Select>
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="标签（逗号分隔）" className="px-2 py-1 rounded text-sm flex-1 min-w-[140px]" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
          <button onClick={handleUpload} disabled={uploading || !file} className="btn-primary">{uploading ? '上传中…' : '上传'}</button>
        </div>
      </div>

      {/* 类别管理 */}
      <div className="card mb-4">
        <button onClick={() => setShowCats((s) => !s)} className="card-title !mb-0 flex items-center gap-2 w-full text-left"><Tags size={16} /> 类别管理（{cats.length}）</button>
        {showCats && (
          <div className="mt-3">
            <div className="flex flex-wrap gap-2 items-center mb-3">
              <input value={newCatKey} onChange={(e) => setNewCatKey(e.target.value)} placeholder="key（英文，如 trap）" className="px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', width: 160 }} />
              <input value={newCatLabel} onChange={(e) => setNewCatLabel(e.target.value)} placeholder="显示名（如 陷阱）" className="px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)', width: 140 }} />
              <button onClick={addCategory} disabled={!newCatKey.trim()} className="btn-secondary text-sm flex items-center gap-1"><Plus size={13} /> 新增类别</button>
            </div>
            <div className="flex flex-wrap gap-2">
              {cats.map((cat) => (
                <div key={cat.key} className="flex items-center gap-1 px-2 py-1 rounded text-xs" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}>
                  {cat.builtin ? (
                    <span>{cat.label} <span style={{ opacity: 0.5 }}>（内置）</span></span>
                  ) : (
                    <>
                      <input defaultValue={cat.label} onBlur={(e) => { if (e.target.value.trim() && e.target.value !== cat.label) renameCategory(cat.key, e.target.value.trim()) }}
                        className="bg-transparent w-16 outline-none" style={{ borderBottom: '1px dashed var(--color-border)' }} />
                      <span style={{ opacity: 0.5 }}>{cat.key}</span>
                      <ConfirmDialog title="删除类别" description={`删除类别「${cat.label}」？（该类别下有素材时无法删除）`} confirmLabel="删除" onConfirm={() => deleteCategory(cat.key)}>
                        {(open) => <button onClick={open} style={{ color: 'var(--color-danger)' }}><Trash2 size={11} /></button>}
                      </ConfirmDialog>
                    </>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* 列表 */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>筛选</span>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            {cats.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}
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
              <button onClick={() => openDetail(a)} title="查看/编辑详情" className="rounded mb-1 flex items-center justify-center w-full cursor-pointer" style={{ height: 80, background: 'repeating-conic-gradient(#0001 0% 25%, transparent 0% 50%) 50%/16px 16px, var(--color-bg-tertiary)' }}>
                <img src={mediaUrl(a.image_url)} alt={a.name} style={{ maxWidth: '90%', maxHeight: 72, imageRendering: 'pixelated' }} />
              </button>
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
                {a.is_default && <span className="badge !text-[10px]" style={{ color: 'var(--color-success)', borderColor: 'var(--color-success)' }}>默认</span>}
              </div>
              {a.tags.length > 0 && <div className="text-[10px] mt-0.5 truncate" style={{ color: 'var(--color-text-secondary)' }}>{a.tags.join('、')}</div>}
            </div>
          ))}
        </div>
      )}

      {/* 素材详情弹窗 */}
      {detail && (
        <div onClick={() => setDetail(null)} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', zIndex: 50, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={(e) => e.stopPropagation()} className="card" style={{ width: 360, maxWidth: '90vw' }}>
            <div className="flex items-center justify-between mb-2">
              <h3 className="card-title !mb-0 flex items-center gap-1"><Pencil size={14} /> 编辑素材</h3>
              <button onClick={() => setDetail(null)} style={{ color: 'var(--color-text-secondary)' }}><X size={16} /></button>
            </div>
            <div className="rounded mb-3 flex items-center justify-center" style={{ height: 140, background: 'repeating-conic-gradient(#0001 0% 25%, transparent 0% 50%) 50%/20px 20px, var(--color-bg-tertiary)' }}>
              <img src={mediaUrl(detail.image_url)} alt={detail.name} style={{ maxWidth: '90%', maxHeight: 128, imageRendering: 'pixelated' }} />
            </div>
            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-2">
                <span className="text-xs w-10" style={{ color: 'var(--color-text-secondary)' }}>名称</span>
                <input value={editName} onChange={(e) => setEditName(e.target.value)} className="flex-1 px-2 py-1 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }} />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs w-10" style={{ color: 'var(--color-text-secondary)' }}>类别</span>
                <Select value={editKind} onValueChange={setEditKind}>
                  <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
                  <SelectContent>{cats.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}</SelectContent>
                </Select>
                {detail.is_default && <span className="badge !text-[10px]" style={{ color: 'var(--color-success)', borderColor: 'var(--color-success)' }}>默认</span>}
              </div>
              <div className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {detail.tags.length > 0 && <span>标签：{detail.tags.join('、')}　</span>}
                {detail.license && <span>许可：{detail.license}</span>}
              </div>
            </div>
            <div className="flex gap-2 mt-3 flex-wrap">
              <button onClick={saveEdit} disabled={editName === detail.name && editKind === detail.kind} className="btn-primary text-sm" style={(editName === detail.name && editKind === detail.kind) ? { opacity: 0.5 } : undefined}>保存修改</button>
              <button onClick={() => setDefault(detail)} disabled={detail.is_default} className="btn-secondary text-sm" style={detail.is_default ? { opacity: 0.5 } : undefined}>
                {detail.is_default ? '已是该类默认' : '设为该类默认'}
              </button>
              {!detail.builtin && (
                <ConfirmDialog title="删除素材" description={`确定删除「${detail.name}」？`} confirmLabel="删除" onConfirm={() => remove(detail.id)}>
                  {(open) => <button onClick={open} className="btn-secondary text-sm flex items-center gap-1 ml-auto" style={{ color: 'var(--color-danger)' }}><Trash2 size={13} /> 删除</button>}
                </ConfirmDialog>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
