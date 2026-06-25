import { create } from 'zustand'
import { api } from '../api/client'

interface Module {
  id: string
  title: string
  rule_system: string
  description: string
  scenes: Array<Record<string, unknown>>
  npcs: Array<Record<string, unknown>>
  clues: Array<Record<string, unknown>>
}

interface ModuleStore {
  modules: Module[]
  currentModule: Module | null
  loading: boolean
  fetchModules: () => Promise<void>
  uploadModule: (file: File, ruleSystem: string) => Promise<void>
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

  uploadModule: async (file, ruleSystem) => {
    set({ loading: true })
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`/api/modules/upload?rule_system=${ruleSystem}`, {
      method: 'POST',
      body: form,
    })
    if (!res.ok) throw new Error(await res.text())
    const modules = await api.get<Module[]>('/modules')
    set({ modules, loading: false })
  },

  selectModule: (module) => set({ currentModule: module }),
}))
