import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'

export function AppShell() {
  // 路由切换整页淡入（150ms）：用 pathname 作 key，切页即重挂载触发 route-fade。
  const { pathname } = useLocation()
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main key={pathname} className="flex-1 overflow-auto p-6 route-fade">
        <Outlet />
      </main>
    </div>
  )
}
