// 模组难度枚举（与后端 module_service.MODULE_DIFFICULTIES 对齐）
export const MODULE_DIFFICULTIES = ['入门', '普通', '困难', '噩梦'] as const
export type ModuleDifficulty = (typeof MODULE_DIFFICULTIES)[number]

/** 从 world_setting.player_count（如 "1-4"、"2-6人"）解析推荐人数范围。 */
export function parsePlayerRange(ws?: Record<string, unknown> | null): { min: number; max: number } {
  const raw = String((ws?.player_count as string | undefined) ?? '')
  const nums = (raw.match(/\d+/g) || []).map(Number).filter((n) => n > 0)
  if (nums.length === 0) return { min: 1, max: 6 }
  if (nums.length === 1) return { min: nums[0], max: nums[0] }
  return { min: Math.min(...nums), max: Math.max(...nums) }
}
