# 沙盘地貌纹理

以下 11 张 `.webp` 纹理由 `gpt-image-2` 生成，用于沙盘六角格地貌渲染。

| 文件 | 地貌 | 说明 |
|------|------|------|
| `plain.webp` | 原野 | 开阔草地，散落小花 |
| `forest.webp` | 密林 | 深色森林树冠俯视 |
| `water.webp` | 水域 | 平静水面波纹 |
| `coast.webp` | 海岸 | 沙滩与浅水交界 |
| `desert.webp` | 荒漠 | 沙丘纹理 |
| `mountain.webp` | 山地 | 岩石山脊 |
| `swamp.webp` | 沼泽 | 泥沼湿地 |
| `urban.webp` | 城镇 | 中世纪鹅卵石路面 |
| `ruin.webp` | 废墟 | 古老碎裂石砖 |
| `interior.webp` | 室内 | 木地板/石地板 |
| `road.webp` | 道路 | 土路与车辙 |

纹理通过 `src/lib/biome.ts` 中的 `BIOME_TEXTURES` 映射，由 `HexSandbox` 组件
以 `soft-light` 叠加模式应用到各六角格。
