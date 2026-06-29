# 地图瓦片资产（放置说明）

地图渲染器（`src/components/module/MapView.tsx`）会在此目录寻找像素瓦片图；缺图时回退到占位色块。

## 请放置 0x72 DungeonTileset II（CC0）

1. 打开官方页面 https://0x72.itch.io/dungeontileset-ii
2. 点 **Download** → 「No thanks, just take me to the downloads」→ 下载 zip（如 `0x72_DungeonTilesetII_v1.7.zip`）。
3. 解压，取其中的**整张瓦片图** `0x72_DungeonTilesetII_v1.7.png`（不是逐帧小图文件夹）。
4. 重命名并放到本目录：`apps/web/public/tiles/dungeon.png`
5. 告诉助手「放好了」，助手会对照真实图谱写好 glyph→精灵坐标与渲染。

## 许可

0x72 DungeonTileset II 由 Robert Norenberg（0x72）以 **CC0 1.0**（公共领域）发布，
可自由用于商业/非商业项目、无需署名。来源：https://0x72.itch.io/dungeontileset-ii
