/**
 * 主题切换（纯前端，裸 localStorage，无状态库）
 * - 'gothic'    ：克苏鲁哥特暗色（默认，:root 基线）
 * - 'parchment' ：羊皮纸暖褐（:root[data-theme="parchment"] 覆盖）
 *
 * 首帧防白闪由 index.html 内联脚本负责（在 #root 渲染前设好 data-theme）。
 * 本模块承载运行时读/写/应用，供 Settings 等页面调用。
 */

export type Theme = 'gothic' | 'parchment'

const STORAGE_KEY = 'trpg_theme'
const DEFAULT_THEME: Theme = 'gothic'

/** 各主题的 <meta name="theme-color"> 值（与 body 顶部底色一致，移动端地址栏配色） */
const META_THEME_COLOR: Record<Theme, string> = {
  gothic: '#0c0e13',
  parchment: '#f0e6d3',
}

export const THEMES: { value: Theme; label: string; swatch: string[] }[] = [
  { value: 'gothic', label: '暗夜哥特', swatch: ['#0c0e13', '#14171f', '#d4a24e'] },
  { value: 'parchment', label: '羊皮纸', swatch: ['#f0e6d3', '#e8dcc8', '#8b2500'] },
]

export function getTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'parchment' || stored === 'gothic' ? stored : DEFAULT_THEME
}

/** 把主题应用到 DOM（写 data-theme + 同步 meta theme-color），不落库 */
export function applyTheme(theme: Theme): void {
  if (theme === DEFAULT_THEME) {
    delete document.documentElement.dataset.theme
  } else {
    document.documentElement.dataset.theme = theme
  }
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', META_THEME_COLOR[theme])
}

/** 持久化 + 立即应用 */
export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme)
  applyTheme(theme)
}
