import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'

export function AppShell() {
  // 路由切换整页淡入（150ms）：用 pathname 作 key，切页即重挂载触发 route-fade。
  const { pathname } = useLocation()
  const gameSession = pathname.startsWith('/game/')
  return (
    <div className={`app-shell flex h-screen overflow-hidden ${gameSession ? 'game-session-shell' : ''}`}>
      <Sidebar />
      <main key={pathname} className="app-main min-w-0 flex-1 overflow-auto p-6 route-fade">
        <Outlet />
      </main>
    </div>
  )
}
