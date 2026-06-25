import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { HomePage } from './pages/HomePage'
import { ModulePage } from './pages/ModulePage'
import { CharacterPage } from './pages/CharacterPage'
import { GamePage } from './pages/GamePage'
import './index.css'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<HomePage />} />
          <Route path="modules" element={<ModulePage />} />
          <Route path="characters" element={<CharacterPage />} />
          <Route path="game" element={<GamePage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
