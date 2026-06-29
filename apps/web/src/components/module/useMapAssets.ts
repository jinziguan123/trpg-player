import { useEffect, useState } from 'react'
import { api, mediaUrl } from '../../api/client'
import type { AssetLite } from './MapView'

interface AssetRow { id: string; kind: string; image_url: string; name: string }

/** 拉取素材库并解析成可直接喂给 MapView 的列表（image_url 拼成绝对地址）。失败回退空数组。 */
export function useMapAssets(): AssetLite[] {
  const [assets, setAssets] = useState<AssetLite[]>([])
  useEffect(() => {
    let on = true
    api.get<AssetRow[]>('/assets')
      .then((rows) => { if (on) setAssets(rows.map((r) => ({ id: r.id, kind: r.kind, image_url: mediaUrl(r.image_url), name: r.name }))) })
      .catch(() => { /* 素材库不可用（如后端未迁移）时回退色块 */ })
    return () => { on = false }
  }, [])
  return assets
}
