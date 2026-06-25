import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useModuleStore } from '../stores/moduleStore'
import { GiUpCard, GiScrollUnfurled, GiReturnArrow } from 'react-icons/gi'

export function ModulePage() {
  const { modules, loading, fetchModules, uploadModule } = useModuleStore()
  const fileRef = useRef<HTMLInputElement>(null)
  const [ruleSystem, setRuleSystem] = useState('coc')
  const [uploading, setUploading] = useState(false)

  useEffect(() => {
    fetchModules()
  }, [fetchModules])

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      await uploadModule(file, ruleSystem)
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const navigate = useNavigate()

  return (
    <div className="max-w-3xl">
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => navigate(-1)} className="btn-secondary flex items-center gap-1 !px-2 !py-1 text-sm">
          <GiReturnArrow /> 返回
        </button>
        <h2 className="page-title !mb-0">模组管理</h2>
      </div>

      <div className="card mb-8">
        <h3 className="card-title flex items-center gap-2">
          <GiUpCard /> 上传模组
        </h3>
        <div className="flex gap-3 items-end">
          <div className="flex-1">
            <input ref={fileRef} type="file" accept=".txt,.md" className="w-full text-sm" />
          </div>
          <select value={ruleSystem} onChange={(e) => setRuleSystem(e.target.value)} className="input">
            <option value="coc">CoC</option>
            <option value="dnd">DnD</option>
          </select>
          <button onClick={handleUpload} disabled={uploading} className="btn-primary">
            {uploading ? '解析中...' : '上传'}
          </button>
        </div>
      </div>

      {loading ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>加载中...</p>
      ) : modules.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>暂无模组，请上传</p>
      ) : (
        <div className="space-y-3">
          {modules.map((m) => (
            <div key={m.id} className="card cursor-pointer hover:border-[var(--color-accent)] transition-colors">
              <div className="flex items-center justify-between mb-1">
                <h3 className="card-title !mb-0 flex items-center gap-2">
                  <GiScrollUnfurled className="opacity-60" /> {m.title}
                </h3>
                <span className="badge">{m.rule_system.toUpperCase()}</span>
              </div>
              <p className="text-sm mt-1" style={{ color: 'var(--color-text-secondary)' }}>
                {m.description}
              </p>
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
