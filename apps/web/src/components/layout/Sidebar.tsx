import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import { GiScrollUnfurled, GiCharacter, GiDiceTwentyFacesTwenty, GiArchiveResearch, GiGears, GiBookCover, GiStoneBlock } from 'react-icons/gi'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'

const NAV_ITEMS = [
  { to: '/', label: '卷宗', icon: GiArchiveResearch },
  { to: '/modules', label: '模组', icon: GiScrollUnfurled },
  { to: '/rulebooks', label: '规则书', icon: GiBookCover },
  { to: '/assets', label: '素材', icon: GiStoneBlock },
  { to: '/characters', label: '角色', icon: GiCharacter },
  { to: '/game', label: '游戏', icon: GiDiceTwentyFacesTwenty },
  { to: '/settings', label: '设置', icon: GiGears },
]

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem('trpg_sidebar_collapsed') === '1',
  )
  const toggle = () => {
    setCollapsed((c) => {
      const next = !c
      localStorage.setItem('trpg_sidebar_collapsed', next ? '1' : '0')
      return next
    })
  }

  return (
    <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-logo" title="TRPG Player">
        {collapsed ? 'T' : 'TRPG Player'}
      </div>
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            title={label}
            className={({ isActive }) =>
              `sidebar-link ${isActive ? 'active' : ''}`
            }
          >
            <Icon />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <button
        className="sidebar-toggle"
        onClick={toggle}
        title={collapsed ? '展开菜单' : '收起菜单'}
      >
        {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        <span>收起</span>
      </button>
      <div
        className="sidebar-footer"
        style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
      >
        {collapsed ? 'v0' : 'v0.1.0'}
      </div>
    </aside>
  )
}
