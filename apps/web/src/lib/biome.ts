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
  'road',
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
  road: '道路',
}

/** 沙盘地貌专属纹理（gpt-image-2 生成），不产生运行时外部网络请求。 */
export const BIOME_TEXTURES: Record<string, string> = {
  plain: '/terrain/plain.webp',
  forest: '/terrain/forest.webp',
  water: '/terrain/water.webp',
  coast: '/terrain/coast.webp',
  desert: '/terrain/desert.webp',
  mountain: '/terrain/mountain.webp',
  swamp: '/terrain/swamp.webp',
  urban: '/terrain/urban.webp',
  ruin: '/terrain/ruin.webp',
  interior: '/terrain/interior.webp',
  road: '/terrain/road.webp',
}
