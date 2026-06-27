import { NavLink } from 'react-router-dom'
import { GiScrollUnfurled, GiCharacter, GiDiceTwentyFacesTwenty, GiArchiveResearch, GiGears, GiBookCover } from 'react-icons/gi'

const NAV_ITEMS = [
  { to: '/', label: '卷宗', icon: GiArchiveResearch },
  { to: '/modules', label: '模组', icon: GiScrollUnfurled },
  { to: '/rulebooks', label: '规则书', icon: GiBookCover },
  { to: '/characters', label: '角色', icon: GiCharacter },
  { to: '/game', label: '游戏', icon: GiDiceTwentyFacesTwenty },
  { to: '/settings', label: '设置', icon: GiGears },
]

export function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        TRPG Player
      </div>
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `sidebar-link ${isActive ? 'active' : ''}`
            }
          >
            <Icon />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div
        className="p-3 text-center text-xs border-t"
        style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-secondary)' }}
      >
        v0.1.0
      </div>
    </aside>
  )
}
