import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '../api/client'
import { useModuleStore } from '../stores/moduleStore'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiUpCard, GiScrollUnfurled, GiReturnArrow, GiArchiveResearch } from 'react-icons/gi'
import { Loader2 } from 'lucide-react'

const ALLOWED_EXTS = ['txt', 'md', 'pdf', 'docx', 'doc', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']

export function ModulePage() {
  const { modules, loading, fetchModules, startUpload } = useModuleStore()
  const fileRef = useRef<HTMLInputElement>(null)
  const [ruleSystem, setRuleSystem] = useState('coc')
  const [uploading, setUploading] = useState(false)
  const [uploadJob, setUploadJob] = useState<{ stage: string; percent: number } | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])

  useEffect(() => {
    fetchModules()
  }, [fetchModules])

  // 有模组在建原文索引时轮询刷新，直到 indexing → ready/failed
  useEffect(() => {
    if (!modules.some((m) => m.rag_status === 'indexing')) return
    const t = setTimeout(() => fetchModules(), 3000)
    return () => clearTimeout(t)
  }, [modules, fetchModules])

  const rebuildRag = async (id: string) => {
    try {
      await api.post(`/modules/${id}/rag/rebuild`)
      toast.success('已开始重建原文索引')
      fetchModules()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '重建索引失败')
    }
  }

  const addFiles = (fileList: FileList | File[]) => {
    const valid: File[] = []
    for (const file of Array.from(fileList)) {
      const ext = file.name.split('.').pop()?.toLowerCase()
      if (!ALLOWED_EXTS.includes(ext || '')) {
        toast.error(`「${file.name}」格式不支持，仅支持 ${ALLOWED_EXTS.map(e => '.' + e).join('、')}`)
        continue
      }
      valid.push(file)
    }
    if (valid.length) setSelectedFiles((prev) => [...prev, ...valid])
  }

  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index))
  }

  const handleUpload = async () => {
    if (!selectedFiles.length) return
    setUploading(true)
    try {
      const jobId = await startUpload(selectedFiles, ruleSystem)
      setSelectedFiles([])
      if (fileRef.current) fileRef.current.value = ''
      setUploadJob({ stage: '排队中', percent: 0 })
      // 轮询后台解析任务进度，直到 done / failed
      type JobStatus = {
        status: 'running' | 'done' | 'failed'
        stage: string
        percent: number
        detail: string
        result: { title?: string } | null
      }
      for (;;) {
        const s = await api.get<JobStatus>(`/modules/upload/status/${jobId}`)
        if (s.status === 'running') {
          setUploadJob({ stage: s.stage, percent: s.percent })
          await new Promise((r) => setTimeout(r, 1200))
          continue
        }
        if (s.status === 'done') {
          toast.success(`模组「${s.result?.title ?? ''}」解析完成`)
          await fetchModules()
        } else {
          toast.error(s.detail || '模组解析失败')
        }
        break
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '模组上传失败')
    } finally {
      setUploadJob(null)
      setUploading(false)
    }
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files)
  }, [])

  const navigate = useNavigate()

  const deleteModule = async (id: string) => {
    try {
      await api.delete(`/modules/${id}`)
      fetchModules()
      toast.success('模组已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const totalSize = selectedFiles.reduce((s, f) => s + f.size, 0)

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0">模组管理</h2>
        <button onClick={() => navigate('/modules/new')} className="ml-auto btn-primary flex items-center gap-1 text-sm">
          <GiUpCard /> 新建模组
        </button>
      </div>

      <div className="card mb-8">
        <h3 className="card-title flex items-center gap-2">
          <GiUpCard /> 上传模组
        </h3>

        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => fileRef.current?.click()}
          className="border-2 border-dashed rounded-md p-6 mb-3 text-center cursor-pointer transition-colors"
          style={{
            borderColor: dragOver ? 'var(--color-accent)' : 'var(--color-border)',
            background: dragOver ? 'rgba(212, 162, 78, 0.07)' : 'rgba(255, 255, 255, 0.03)',
          }}
        >
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.md,.pdf,.docx,.doc,.png,.jpg,.jpeg,.webp,.gif,.bmp,image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files?.length) addFiles(e.target.files)
              e.target.value = ''
            }}
          />
          {selectedFiles.length > 0 ? (
            <div>
              <p className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>
                已选择 {selectedFiles.length} 个文件
              </p>
              <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                共 {(totalSize / 1024).toFixed(1)} KB · 点击继续添加
              </p>
            </div>
          ) : (
            <div>
              <GiUpCard className="mx-auto text-2xl mb-2" style={{ color: 'var(--color-text-secondary)' }} />
              <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                拖拽文件到此处，或点击选择（可多选）
              </p>
              <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>
                支持 .txt、.md、.pdf、.docx、.doc、图片(png/jpg…) · 多个文件视为同一模组（图片走视觉模型识别）
              </p>
            </div>
          )}
        </div>

        {selectedFiles.length > 0 && (
          <div className="mb-3 space-y-1">
            {selectedFiles.map((f, i) => (
              <div
                key={`${f.name}-${i}`}
                className="flex items-center justify-between px-2 py-1 rounded text-sm"
                style={{ background: 'var(--color-bg-tertiary)' }}
              >
                <span className="truncate flex-1 mr-2">{f.name}</span>
                <span className="text-xs mr-2 flex-shrink-0" style={{ color: 'var(--color-text-secondary)' }}>
                  {(f.size / 1024).toFixed(1)} KB
                </span>
                <button
                  onClick={(e) => { e.stopPropagation(); removeFile(i) }}
                  className="text-xs px-1 rounded hover:bg-[var(--color-danger-deep)] hover:text-white transition-colors"
                  style={{ color: 'var(--color-danger)' }}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-3 items-center">
          <Select value={ruleSystem} onValueChange={setRuleSystem}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="coc">CoC</SelectItem>
              <SelectItem value="dnd">DnD</SelectItem>
            </SelectContent>
          </Select>
          <button onClick={handleUpload} disabled={uploading || !selectedFiles.length} className="btn-primary">
            {uploading ? '解析中...' : '上传并解析'}
          </button>
        </div>
      </div>

      {uploadJob && (
        <div className="card" style={{ padding: '0.75rem 1rem', marginBottom: '1rem' }}>
          <div
            style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              fontSize: '0.82rem', color: 'var(--color-text-secondary)', marginBottom: '0.4rem',
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4rem' }}>
              <Loader2 size={13} className="animate-spin" />
              {uploadJob.stage}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)' }}>{uploadJob.percent}%</span>
          </div>
          <div className="upload-progress-track">
            <div
              className="upload-progress-fill"
              style={{ width: `${Math.max(uploadJob.percent, 4)}%` }}
            />
          </div>
        </div>
      )}

      {loading ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>加载中...</p>
      ) : modules.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>暂无模组，请上传</p>
      ) : (
        <div className="space-y-3">
          {modules.map((m) => (
            <div key={m.id} className="card hover:border-[var(--color-accent)] transition-colors">
              <div className="flex items-center justify-between mb-1">
                <h3 className="card-title !mb-0 flex items-center gap-2">
                  <GiScrollUnfurled className="opacity-60" /> {m.title}
                </h3>
                <div className="flex items-center gap-2">
                  <span className="badge">{m.rule_system.toUpperCase()}</span>
                  {m.rag_status === 'ready' && (
                    <span className="badge flex items-center gap-1" title="模组原文已建索引，跑团时 KP 可引用原文">
                      <GiArchiveResearch /> 原文索引
                    </span>
                  )}
                  {m.rag_status === 'indexing' && (
                    <span className="badge flex items-center gap-1" title="正在为模组原文建索引">
                      <Loader2 className="animate-spin" size={12} /> 索引中
                    </span>
                  )}
                  {m.rag_status === 'failed' && (
                    <span
                      className="badge flex items-center gap-1"
                      style={{ background: 'var(--color-danger)', color: '#fff' }}
                      title="原文索引构建失败，可点「重建索引」重试"
                    >
                      <GiArchiveResearch /> 索引失败
                    </span>
                  )}
                  {m.rag_status !== 'indexing' && (
                    <button
                      onClick={() => rebuildRag(m.id)}
                      className="text-xs px-1.5 py-0.5 rounded transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-on-accent)] flex items-center gap-1"
                      style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-border)' }}
                      title="（重）建模组原文索引：让 KP 跑团时能检索并引用模组原文"
                    >
                      <GiArchiveResearch /> 重建索引
                    </button>
                  )}
                  <ConfirmDialog
                    title="查看 / 编辑模组（含剧透）"
                    description={`「${m.title}」的内容包含 NPC 秘密、线索与剧情真相。若你打算亲自游玩本模组，请不要查看。确定继续吗？`}
                    confirmLabel="继续查看"
                    onConfirm={() => navigate(`/modules/${m.id}`)}
                  >
                    {(open) => (
                      <button
                        onClick={open}
                        className="text-xs px-1.5 py-0.5 rounded transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-on-accent)]"
                        style={{ color: 'var(--color-text-accent)', border: '1px solid var(--color-border)' }}
                      >
                        查看/编辑
                      </button>
                    )}
                  </ConfirmDialog>
                  <ConfirmDialog
                    title="删除模组"
                    description={`确定要删除「${m.title}」吗？此操作不可恢复。`}
                    confirmLabel="删除"
                    onConfirm={() => deleteModule(m.id)}
                  >
                    {(open) => (
                      <button
                        onClick={open}
                        className="text-xs px-1.5 py-0.5 rounded hover:bg-[var(--color-danger-deep)] hover:text-white transition-colors"
                        style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}
                      >
                        删除
                      </button>
                    )}
                  </ConfirmDialog>
                </div>
              </div>
              <p className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                {m.description}
              </p>
              <div className="flex flex-wrap gap-1.5 mt-2">
                {Boolean(m.world_setting?.era) && (
                  <span className="badge">{String(m.world_setting.era)}</span>
                )}
                {Boolean(m.world_setting?.region) && (
                  <span className="badge">{String(m.world_setting.region)}</span>
                )}
                {Boolean(m.world_setting?.player_count) && (
                  <span className="badge">{String(m.world_setting.player_count)}人</span>
                )}
                {Boolean(m.world_setting?.difficulty) && (
                  <span
                    className="badge"
                    style={{
                      background: ({ '入门': '#2d7d46', '普通': '#6b7280', '困难': '#b45309', '噩梦': '#991b1b' } as Record<string, string>)[String(m.world_setting.difficulty)] || 'var(--color-accent)',
                      color: '#fff',
                    }}
                  >
                    {String(m.world_setting.difficulty)}
                  </span>
                )}
                {(m.world_setting?.tags as string[] || []).map((t: string) => (
                  <span key={t} className="badge" style={{ opacity: 0.8 }}>{t}</span>
                ))}
              </div>
              <div className="flex gap-4 mt-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                <span>{m.scenes?.length ?? 0} 个场景</span>
                <span>{m.npcs?.length ?? 0} 个 NPC</span>
                <span>{m.clues?.length ?? 0} 条线索</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
