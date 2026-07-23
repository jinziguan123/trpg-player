export const BIOMES = [
  'plain',
  'forest',
  'water',
  'coast',
  'desert',
  'mountain',
  'swamp',
  'urban',
  'ruin',
  'interior',
] as const

export const BIOME_LABELS: Record<string, string> = {
  plain: '原野',
  forest: '密林',
  water: '水域',
  coast: '海岸',
  desert: '荒漠',
  mountain: '山地',
  swamp: '沼泽',
  urban: '城镇',
  ruin: '废墟',
  interior: '室内',
}
