import { useEffect, useState } from 'react'
import { api } from '../../api/client'

export interface SpecItem { name: string; init: number }
export interface SpecCategory { base_init: number; items: SpecItem[] }
export interface Specializations {
  categories: Record<string, SpecCategory>
  single: string[]   // 唯一专精（母语），值固定为 EDU
}

export interface WeaponDef {
  name: string
  skill: string
  dam: string
  tho: number       // 0/1 是否贯穿
  range: string
  round: string     // 次数 a/b
  num: string       // 装弹量
  price: string
  err: string       // 故障率
  time: string
  category: string  // 大类：常规/手枪/半自动步枪/…/其他
}

export interface WeaponData {
  weapons: WeaponDef[]
  categories: string[]   // 大类展示顺序
}

// 角色携带的武器（规范字段；兼容历史 damage/attacks/ammo）
export interface CharWeapon {
  name: string
  skill?: string
  success?: number   // 成功率
  dam?: string       // 伤害
  range?: string     // 射程
  tho?: boolean      // 是否贯穿
  round?: string     // 次数 a/b
  num?: string       // 装弹量
  err?: string       // 故障
}

/** 历史武器对象（含旧字段 damage/attacks/ammo）归一化为规范字段。 */
export function normalizeWeapon(w: Record<string, unknown>): CharWeapon {
  return {
    name: String(w.name ?? ''),
    skill: (w.skill as string) ?? '',
    success: (w.success as number) ?? undefined,
    dam: (w.dam as string) ?? (w.damage as string) ?? '',
    range: (w.range as string) ?? '',
    tho: typeof w.tho === 'boolean' ? w.tho : w.tho === 1,
    round: (w.round as string) ?? (w.attacks != null ? String(w.attacks) : ''),
    num: (w.num as string) ?? (w.ammo as string) ?? '',
    err: (w.err as string) ?? '',
  }
}

let _specCache: Specializations | null = null
let _weaponCache: WeaponData | null = null

/** 专精类别（母语/外语/格斗/射击/科学/生存/技艺/驾驶），进程内缓存。 */
export function useSpecializations() {
  const [data, setData] = useState<Specializations | null>(_specCache)
  useEffect(() => {
    if (_specCache) return
    api.get<Specializations>('/rules/coc/specializations')
      .then((d) => { _specCache = d; setData(d) })
      .catch(() => setData({ categories: {}, single: [] }))
  }, [])
  return data
}

/** CoC 武器表 + 大类顺序，进程内缓存。 */
export function useWeapons() {
  const [data, setData] = useState<WeaponData | null>(_weaponCache)
  useEffect(() => {
    if (_weaponCache) return
    api.get<WeaponData>('/rules/coc/weapons')
      .then((d) => { _weaponCache = d; setData(d) })
      .catch(() => setData({ weapons: [], categories: [] }))
  }, [])
  return data
}

/** 把技能键拆成基名与专精，如 "格斗(斗殴)" → ["格斗","斗殴"]；无专精则 spec 为空。 */
export function splitSkill(key: string): [string, string] {
  const m = key.match(/^(.+?)\((.+)\)$/)
  return m ? [m[1], m[2]] : [key, '']
}
