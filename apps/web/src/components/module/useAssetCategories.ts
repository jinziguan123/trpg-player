import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api/client'

export interface AssetCategory { key: string; label: string; builtin: boolean }

/** 拉取素材类别（内置 + 自定义）。返回列表 + 重新加载函数。 */
export function useAssetCategories(): [AssetCategory[], () => void] {
  const [cats, setCats] = useState<AssetCategory[]>([])
  const reload = useCallback(() => {
    api.get<AssetCategory[]>('/asset-categories').then(setCats).catch(() => { /* 静默 */ })
  }, [])
  useEffect(() => { reload() }, [reload])
  return [cats, reload]
}
