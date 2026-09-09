import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import type { User } from '../api'

interface AuthState {
  user: User | null
  loading: boolean
  isAdmin: boolean
  refresh: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState>({
  user: null, loading: true, isAdmin: false,
  refresh: async () => {}, signOut: async () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      // 401 由 axios 拦截器处理跳转，这里只需清空状态
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  const signOut = useCallback(async () => {
    let endSessionUrl: string | null = null
    if (user?.auth_source === 'oidc') {
      // 统一登录的账号同时结束 IdP 会话，否则会被立刻静默重新登录
      endSessionUrl = await api.oidcLogoutUrl().then((r) => r.end_session_url).catch(() => null)
    } else {
      await api.logout().catch(() => undefined)
    }
    setUser(null)
    location.href = endSessionUrl ?? '/login'
  }, [user])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const value = useMemo<AuthState>(
    () => ({ user, loading, isAdmin: user?.role === 'admin', refresh, signOut }),
    [user, loading, refresh, signOut],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export const useAuth = () => useContext(AuthContext)
