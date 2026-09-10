import { useEffect, useMemo, useRef, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Alert, Avatar, Dropdown, Layout, Menu, Tag, Typography } from 'antd'
import {
  ApiOutlined, CodeOutlined, ControlOutlined, DatabaseOutlined, FileTextOutlined, LogoutOutlined,
  MessageOutlined, PlusOutlined, SettingOutlined, TeamOutlined, UserOutlined,
} from '@ant-design/icons'
import { api } from '../api'
import { useAuth } from '../hooks/useAuth'

const { Header, Sider, Content } = Layout

export default function MainLayout() {
  const { user, isAdmin, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const contentRef = useRef<HTMLDivElement>(null)

  // 现在滚动发生在容器里，切页面不会像整页滚动那样自动回到顶部，
  // 不重置的话从列表往下滚再点进详情，看到的会是半截页面
  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0 })
  }, [location.pathname])

  useEffect(() => {
    api.settings().then((s) => setAnnouncement(s.announcement)).catch(() => undefined)
  }, [])

  const items = useMemo(() => {
    const base = [
      { key: '/jobs', icon: <DatabaseOutlined />, label: '任务列表' },
      { key: '/jobs/new', icon: <PlusOutlined />, label: '新建任务' },
      {
        key: '/tools', icon: <CodeOutlined />, label: '数据预处理',
        children: [
          { key: '/tools/split-script', icon: <FileTextOutlined />, label: '脚本生成' },
          { key: '/tools/prompts', icon: <MessageOutlined />, label: '我的 Prompt' },
        ],
      },
    ]
    if (!isAdmin) return base
    return [
      ...base,
      { type: 'divider' as const },
      { key: '/admin/models', icon: <ApiOutlined />, label: '模型配置' },
      { key: '/admin/users', icon: <TeamOutlined />, label: '用户管理' },
      { key: '/admin/system', icon: <ControlOutlined />, label: '系统与队列' },
    ]
  }, [isAdmin])

  // /jobs/:id 也要让「任务列表」保持选中
  const selectedKey = location.pathname.startsWith('/jobs/') && location.pathname !== '/jobs/new'
    ? '/jobs'
    : location.pathname

  return (
    <Layout className="app-shell">
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed} theme="dark">
        {/* 纵向 flex：菜单区自己滚，避免菜单变长时把折叠按钮顶走 */}
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ height: 56, flex: '0 0 auto', display: 'flex', alignItems: 'center',
            justifyContent: 'center', color: '#fff', fontWeight: 600, letterSpacing: 1 }}>
            {collapsed ? 'BI' : '批量推理平台'}
          </div>
          <div style={{ flex: '1 1 auto', overflow: 'auto' }}>
            <Menu
              theme="dark"
              mode="inline"
              selectedKeys={[selectedKey]}
              // 子菜单默认展开，数据预处理下的两个入口一眼就能看到
              defaultOpenKeys={['/tools']}
              items={items}
              onClick={({ key }) => navigate(key)}
            />
          </div>
        </div>
      </Sider>
      <Layout style={{ height: '100%', overflow: 'hidden' }}>
        <Header style={{ background: 'transparent', display: 'flex', alignItems: 'center',
          justifyContent: 'flex-end', gap: 12, paddingInline: 24, flex: '0 0 auto' }}>
          {isAdmin && <Tag color="gold">管理员</Tag>}
          <Dropdown
            menu={{
              items: [
                { key: 'profile', icon: <SettingOutlined />, label: '个人设置' },
                { type: 'divider' },
                { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
              ],
              onClick: ({ key }) => (key === 'logout' ? void signOut() : navigate('/profile')),
            }}
          >
            <span style={{ cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Avatar size="small" icon={<UserOutlined />} />
              <Typography.Text>{user?.display_name || user?.username}</Typography.Text>
            </span>
          </Dropdown>
        </Header>
        {/* 全站唯一的滚动区域。用 padding 而不是 margin，滚动条才贴在最右边 */}
        <Content ref={contentRef} className="app-scroll"
          style={{ padding: '0 24px 24px', overflow: 'auto', flex: '1 1 auto' }}>
          {announcement && (
            <Alert type="info" showIcon banner message={announcement} style={{ marginBottom: 16 }} />
          )}
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
