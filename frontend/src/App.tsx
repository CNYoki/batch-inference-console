import { Navigate, Route, Routes } from 'react-router-dom'
import { Spin } from 'antd'
import type { ReactElement } from 'react'
import { useAuth } from './hooks/useAuth'
import MainLayout from './layouts/MainLayout'
import LoginPage from './pages/LoginPage'
import JobsPage from './pages/JobsPage'
import JobDetailPage from './pages/JobDetailPage'
import NewJobPage from './pages/NewJobPage'
import ProfilePage from './pages/ProfilePage'
import ScriptGeneratorPage from './pages/ScriptGeneratorPage'
import PromptsPage from './pages/PromptsPage'
import AdminModelsPage from './pages/AdminModelsPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminSystemPage from './pages/AdminSystemPage'

function Guard({ children, adminOnly = false }: { children: ReactElement; adminOnly?: boolean }) {
  const { user, loading, isAdmin } = useAuth()
  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
        <Spin size="large" tip="加载中…" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  if (adminOnly && !isAdmin) return <Navigate to="/jobs" replace />
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <Guard>
            <MainLayout />
          </Guard>
        }
      >
        <Route index element={<Navigate to="/jobs" replace />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="jobs/new" element={<NewJobPage />} />
        <Route path="jobs/:jobId" element={<JobDetailPage />} />
        <Route path="tools/split-script" element={<ScriptGeneratorPage />} />
        <Route path="tools/prompts" element={<PromptsPage />} />
        <Route path="profile" element={<ProfilePage />} />
        <Route
          path="admin/models"
          element={<Guard adminOnly><AdminModelsPage /></Guard>}
        />
        <Route
          path="admin/users"
          element={<Guard adminOnly><AdminUsersPage /></Guard>}
        />
        <Route
          path="admin/system"
          element={<Guard adminOnly><AdminSystemPage /></Guard>}
        />
      </Route>
      <Route path="*" element={<Navigate to="/jobs" replace />} />
    </Routes>
  )
}
