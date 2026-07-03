import { useEffect, useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getApiBase, getPlayerToken } from '../api/client'
import { ConfirmDialog } from '../components/ui/confirm-dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { GiBookCover, GiUpCard, GiReturnArrow, GiMagnifyingGlass } from 'react-icons/gi'

interface Rulebook {
  id: string
  title: string
  rule_system: string
  page_count: number
  chunk_count: number
  status: string
  embed_model: string
  error: string
}

interface RuleHit {
  text: string
  page: number
  score: number
  rulebook_id: string
}

const STATUS_LABEL: Record<string, string> = {
  indexing: '索引中…',
  ready: '可检索',
  failed: '失败',
}
const STATUS_COLOR: Record<string, string> = {
  indexing: '#b45309',
  ready: '#2d7d46',
  failed: '#991b1b',
}

export function RulebookPage() {
  const navigate = useNavigate()
  const fileRef = useRef<HTMLInputElement>(null)
  const [books, setBooks] = useState<Rulebook[]>([])
  const [ruleSystem, setRuleSystem] = useState('coc')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  // 测试检索
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<RuleHit[] | null>(null)
  const [searching, setSearching] = useState(false)

  const fetchBooks = useCallback(async () => {
    try {
      setBooks(await api.get<Rulebook[]>('/rulebooks'))
    } catch {
      /* 静默：列表拉取失败不打扰 */
    }
  }, [])

  useEffect(() => {
    fetchBooks()
  }, [fetchBooks])

  // 有规则书处于索引中时轮询，直到全部 ready/failed
  useEffect(() => {
    if (!books.some((b) => b.status === 'indexing')) return
    const t = setInterval(fetchBooks, 2000)
    return () => clearInterval(t)
  }, [books, fetchBooks])

  const pickFile = (f: File | undefined) => {
    if (!f) return
    if (!f.name.toLowerCase().endsWith('.pdf')) {
      toast.error('规则书目前只支持 PDF')
      return
    }
    setFile(f)
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    pickFile(e.dataTransfer.files[0])
  }, [])

  const handleUpload = async () => {
    if (!file) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const params = new URLSearchParams({ rule_system: ruleSystem, title: file.name.replace(/\.pdf$/i, '') })
      const res = await fetch(`${getApiBase()}/rulebooks/upload?${params.toString()}`, {
        method: 'POST',
        headers: { 'X-Player-Token': getPlayerToken() },
        body: form,
      })
      if (!res.ok) throw new Error(await res.text())
      setFile(null)
      if (fileRef.current) fileRef.current.value = ''
      toast.success('已上传，正在后台建立索引…')
      fetchBooks()
    } catch (e) {
      toast.error(`上传失败：${e instanceof Error ? e.message : '未知错误'}`)
    } finally {
      setUploading(false)
    }
  }

  const deleteBook = async (id: string) => {
    try {
      await api.delete(`/rulebooks/${id}`)
      fetchBooks()
      toast.success('规则书已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const runSearch = async () => {
    const q = query.trim()
    if (!q) return
    setSearching(true)
    try {
      const params = new URLSearchParams({ q, rule_system: ruleSystem, k: '3' })
      const data = await api.get<{ query: string; hits: RuleHit[] }>(`/rulebooks/search?${params.toString()}`)
      setHits(data.hits)
    } catch {
      toast.error('检索失败')
    } finally {
      setSearching(false)
    }
  }

  const hasReady = books.some((b) => b.status === 'ready' && b.rule_system === ruleSystem)

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0">规则书</h2>
      </div>

      <p className="text-sm mb-4" style={{ color: 'var(--color-text-secondary)' }}>
        上传规则书 PDF（如《守秘人规则书》），系统会在本地建立可检索索引。游戏中守秘人遇到拿不准的精确规则时，会按需查阅规则书原文再裁定。
      </p>

      <div className="card mb-8">
        <h3 className="card-title flex items-center gap-2">
          <GiUpCard /> 上传规则书
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
            accept=".pdf"
            className="hidden"
            onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = '' }}
          />
          {file ? (
            <div>
              <p className="text-sm font-semibold" style={{ color: 'var(--color-text-accent)' }}>{file.name}</p>
              <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                {(file.size / 1024 / 1024).toFixed(1)} MB · 点击可重选
              </p>
            </div>
          ) : (
            <div>
              <GiBookCover className="mx-auto text-2xl mb-2" style={{ color: 'var(--color-text-secondary)' }} />
              <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>拖拽 PDF 到此处，或点击选择</p>
              <p className="text-xs mt-1" style={{ color: 'var(--color-text-secondary)', opacity: 0.7 }}>
                仅支持含文字层的 PDF（扫描件需先 OCR）
              </p>
            </div>
          )}
        </div>

        <div className="flex gap-3 items-center">
          <Select value={ruleSystem} onValueChange={setRuleSystem}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="coc">CoC</SelectItem>
              <SelectItem value="dnd">DnD</SelectItem>
            </SelectContent>
          </Select>
          <button onClick={handleUpload} disabled={uploading || !file} className="btn-primary">
            {uploading ? '上传中…' : '上传并索引'}
          </button>
        </div>
      </div>

      {books.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>暂无规则书，请上传</p>
      ) : (
        <div className="space-y-3 mb-8">
          {books.map((b) => (
            <div key={b.id} className="card">
              <div className="flex items-center justify-between mb-1">
                <h3 className="card-title !mb-0 flex items-center gap-2">
                  <GiBookCover className="opacity-60" /> {b.title}
                </h3>
                <div className="flex items-center gap-2">
                  <span className="badge">{b.rule_system.toUpperCase()}</span>
                  <span className="badge" style={{ background: STATUS_COLOR[b.status] || 'var(--color-accent)', color: '#fff' }}>
                    {STATUS_LABEL[b.status] || b.status}
                  </span>
                  <ConfirmDialog
                    title="删除规则书"
                    description={`确定要删除「${b.title}」及其索引吗？此操作不可恢复。`}
                    confirmLabel="删除"
                    onConfirm={() => deleteBook(b.id)}
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
              <div className="flex flex-wrap gap-4 mt-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                <span>{b.page_count} 页</span>
                <span>{b.chunk_count} 个片段</span>
                {b.embed_model && <span>模型 {b.embed_model}</span>}
              </div>
              {b.status === 'failed' && b.error && (
                <p className="text-xs mt-2" style={{ color: 'var(--color-danger)' }}>错误：{b.error}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {hasReady && (
        <div className="card">
          <h3 className="card-title flex items-center gap-2">
            <GiMagnifyingGlass /> 测试检索
          </h3>
          <div className="flex gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') runSearch() }}
              placeholder="输入规则关键词，如「孤注一掷」「理智丧失」"
              className="flex-1 px-3 py-1.5 rounded text-sm"
              style={{ background: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
            />
            <button onClick={runSearch} disabled={searching || !query.trim()} className="btn-secondary">
              {searching ? '检索中…' : '检索'}
            </button>
          </div>
          {hits && (
            <div className="space-y-2 mt-3">
              {hits.length === 0 ? (
                <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>无匹配片段</p>
              ) : (
                hits.map((h, i) => (
                  <div key={i} className="px-3 py-2 rounded text-sm" style={{ background: 'var(--color-bg-tertiary)' }}>
                    <div className="text-xs mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                      第 {h.page} 页 · 相关度 {h.score.toFixed(3)}
                    </div>
                    {h.text}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
