import { create } from 'zustand'
import { api, uploadFile } from '../api/client'

interface Module {
  id: string
  title: string
  rule_system: string
  description: string
  world_setting: Record<string, unknown>
  scenes: Array<Record<string, unknown>>
  npcs: Array<Record<string, unknown>>
  clues: Array<Record<string, unknown>>
  /** 原文 RAG 索引状态：''=未建 / indexing / ready / failed */
  rag_status?: string
}

interface ModuleStore {
  modules: Module[]
  currentModule: Module | null
  loading: boolean
  fetchModules: () => Promise<void>
  /** 提交上传并启动后台解析任务，立即返回 job_id（进度经 /modules/upload/status/{job_id} 轮询） */
  startUpload: (files: File[], ruleSystem: string) => Promise<string>
  selectModule: (module: Module) => void
}

export const useModuleStore = create<ModuleStore>((set) => ({
  modules: [],
  currentModule: null,
  loading: false,

  fetchModules: async () => {
    set({ loading: true })
    const modules = await api.get<Module[]>('/modules')
    set({ modules, loading: false })
  },

  startUpload: async (files, ruleSystem) => {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    const res = await uploadFile<{ job_id: string }>(`/modules/upload?rule_system=${ruleSystem}`, form)
    return res.job_id
  },

  selectModule: (module) => set({ currentModule: module }),
}))
