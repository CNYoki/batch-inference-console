import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Button, Card, Divider, Form, Input, Typography } from 'antd'
import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { AuthInfo } from '../api'
import { useAuth } from '../hooks/useAuth'

export default function LoginPage() {
  const navigate = useNavigate()
  const { user, refresh } = useAuth()
  const [params] = useSearchParams()
  const [info, setInfo] = useState<AuthInfo | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const oidcError = params.get('error')

  useEffect(() => {
    api.authInfo().then(setInfo).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (user) navigate('/jobs', { replace: true })
  }, [user, navigate])

  const onFinish = async (values: { username: string; password: string }) => {
    setSubmitting(true)
    try {
      await api.login(values.username, values.password)
      await refresh()
      navigate('/jobs', { replace: true })
    } catch {
      // 错误提示由 axios 拦截器统一弹出
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'linear-gradient(135deg, #1d39c4 0%, #2f54eb 45%, #597ef7 100%)' }}>
      <Card style={{ width: 400 }} styles={{ body: { padding: 32 } }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 4 }}>
          批量推理平台
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center' }}>
          上传 JSONL，批量调用大模型
        </Typography.Paragraph>

        {oidcError && (
          <Alert type="error" showIcon message="统一登录失败" description={oidcError}
            style={{ marginBottom: 16 }} />
        )}

        {info?.local_auth_enabled !== false && (
          <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
            <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
              <Input prefix={<UserOutlined />} placeholder="用户名" size="large" autoComplete="username" />
            </Form.Item>
            <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large"
                autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" size="large" block loading={submitting}>
              登录
            </Button>
          </Form>
        )}

        {info?.oidc_enabled && (
          <>
            {info.local_auth_enabled && <Divider plain>或</Divider>}
            <Button
              size="large"
              block
              icon={<SafetyCertificateOutlined />}
              onClick={() => {
                // 走后端发起授权码流程，回调后由后端下发会话 Cookie
                location.href = '/api/auth/oidc/login?redirect_after=/jobs'
              }}
            >
              使用{info.oidc_display_name}登录
            </Button>
          </>
        )}

        {info && !info.local_auth_enabled && !info.oidc_enabled && (
          <Alert type="warning" showIcon message="未启用任何登录方式，请联系管理员检查后端配置" />
        )}
      </Card>
    </div>
  )
}
