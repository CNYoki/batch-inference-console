import { useEffect, useState } from 'react'
import {
  App, Button, Card, Descriptions, Form, Input, Progress, Space, Switch, Tag, Typography,
} from 'antd'
import { KeyOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { MyUsage, SystemSettings } from '../api'
import { useAuth } from '../hooks/useAuth'
import { formatBytes, formatDateTime } from '../utils'

export default function ProfilePage() {
  const { user, refresh } = useAuth()
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)

  const [settings, setSettings] = useState<SystemSettings | null>(null)
  const [usage, setUsage] = useState<MyUsage | null>(null)
  const [token, setToken] = useState('')
  const [remember, setRemember] = useState(true)
  const [tokenBusy, setTokenBusy] = useState(false)
  const [notifyBusy, setNotifyBusy] = useState(false)
  const [mailForm] = Form.useForm()

  useEffect(() => {
    api.settings().then(setSettings).catch(() => undefined)
    api.usage().then(setUsage).catch(() => undefined)
  }, [])

  if (!user) return null

  const saveToken = async () => {
    if (!token.trim()) {
      message.warning('请填写 token')
      return
    }
    setTokenBusy(true)
    try {
      const res = await api.personalModels(token.trim(), remember)
      message.success(`token 有效，可用模型 ${res.models.length} 个`)
      setToken('')
      await refresh()
    } catch {
      // 具体原因由拦截器弹出
    } finally {
      setTokenBusy(false)
    }
  }

  const clearToken = async () => {
    setTokenBusy(true)
    try {
      await api.clearPersonalToken()
      message.success('已清除')
      await refresh()
    } finally {
      setTokenBusy(false)
    }
  }

  const changePassword = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await api.changePassword(values.old_password, values.new_password)
      message.success('密码已修改')
      form.resetFields()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%', maxWidth: 720 }}>
      <Card title="账号信息">
        <Descriptions column={1} bordered size="small">
          <Descriptions.Item label="用户名"><span className="mono">{user.username}</span></Descriptions.Item>
          <Descriptions.Item label="显示名">{user.display_name || '—'}</Descriptions.Item>
          <Descriptions.Item label="邮箱">{user.email || '—'}</Descriptions.Item>
          <Descriptions.Item label="角色">
            {user.role === 'admin' ? <Tag color="gold">管理员</Tag> : <Tag>普通用户</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="登录方式">
            {user.auth_source === 'oidc' ? <Tag color="blue">统一登录 (OIDC)</Tag> : <Tag>本地账号</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="并发任务上限">
            {user.max_concurrent_jobs || '不限'}
          </Descriptions.Item>
          <Descriptions.Item label="最近登录">{formatDateTime(user.last_login_at)}</Descriptions.Item>
          {usage && (
            <Descriptions.Item label="存储用量">
              {usage.storage.unlimited ? (
                <>{formatBytes(usage.storage.used)}（不限额）</>
              ) : (
                <Space direction="vertical" size={4} style={{ width: '100%', maxWidth: 320 }}>
                  <span>
                    {formatBytes(usage.storage.used)} / {formatBytes(usage.storage.limit)}
                    {usage.storage.pending > 0 && `（含暂存 ${formatBytes(usage.storage.pending)}）`}
                  </span>
                  <Progress
                    percent={usage.storage.percent} size="small"
                    status={usage.storage.percent >= 90 ? 'exception' : 'normal'}
                  />
                </Space>
              )}
            </Descriptions.Item>
          )}
        </Descriptions>
      </Card>

      <Card title="通知设置">
        {settings?.smtp_enabled ? (
          <Form
            form={mailForm} layout="vertical" style={{ maxWidth: 420 }}
            initialValues={{ notify_email: user.notify_email, email: user.email ?? '' }}
          >
            <Form.Item name="notify_email" label="任务结束后邮件通知我" valuePropName="checked"
              extra="包括任务完成与异常终止；取消任务不会发信">
              <Switch />
            </Form.Item>
            <Form.Item name="email" label="接收邮箱"
              extra={user.auth_source === 'oidc'
                ? '支持自定义接收邮箱'
                : '留空则收不到通知'}>
              <Input type="email" placeholder="you@example.com" />
            </Form.Item>
            <Button
              type="primary" loading={notifyBusy}
              onClick={async () => {
                const values = await mailForm.validateFields()
                setNotifyBusy(true)
                try {
                  await api.updatePreferences({
                    notify_email: values.notify_email,
                    email: values.email || null,
                  })
                  await refresh()
                  message.success('已保存')
                } catch { /* 拦截器已提示 */ } finally {
                  setNotifyBusy(false)
                }
              }}
            >
              保存
            </Button>
          </Form>
        ) : (
          <Typography.Text type="secondary">管理员尚未启用邮件通知功能。</Typography.Text>
        )}
      </Card>

      {settings?.user_gateway_enabled && (
        <Card title={`${settings.user_gateway_label} token`}>
          <Typography.Paragraph type="secondary">
            用于拉取你有权限的模型，并在任务执行时以你的身份调用
            <span className="mono"> {settings.user_gateway_base_url}</span>。
            token 加密保存.
          </Typography.Paragraph>

          {user.llm_token_masked ? (
            <Descriptions column={1} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="当前 token">
                <span className="mono">{user.llm_token_masked}</span>
              </Descriptions.Item>
              <Descriptions.Item label="更新时间">
                {formatDateTime(user.llm_token_updated_at)}
              </Descriptions.Item>
            </Descriptions>
          ) : (
            <Tag style={{ marginBottom: 16 }}>尚未设置</Tag>
          )}

          <Space.Compact style={{ width: '100%', maxWidth: 520 }}>
            <Input.Password
              prefix={<KeyOutlined />} placeholder={user.llm_token_masked ? '填写新 token 以替换' : 'sk-…'}
              value={token} onChange={(e) => setToken(e.target.value)}
              onPressEnter={() => void saveToken()} className="mono"
            />
            <Button type="primary" loading={tokenBusy} onClick={() => void saveToken()}>
              验证并保存
            </Button>
          </Space.Compact>
          <Space style={{ marginTop: 10 }}>
            <Switch size="small" checked={remember} onChange={setRemember} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>保存到服务端</Typography.Text>
            {user.llm_token_masked && (
              <Button size="small" danger loading={tokenBusy} onClick={() => void clearToken()}>
                清除已保存的 token
              </Button>
            )}
          </Space>
          {user.llm_token_masked && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
              清除后，正在排队的「个人模型」任务会因拿不到 token 而失败，需要手动恢复。
            </Typography.Paragraph>
          )}
        </Card>
      )}

      {user.auth_source === 'local' && (
        <Card title="修改密码">
          <Form form={form} layout="vertical" style={{ maxWidth: 380 }}>
            <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Form.Item name="new_password" label="新密码"
              rules={[{ required: true, min: 8, message: '新密码至少 8 位' }]}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item name="confirm" label="确认新密码" dependencies={['new_password']}
              rules={[
                { required: true, message: '请再次输入新密码' },
                ({ getFieldValue }) => ({
                  validator: (_, value) =>
                    !value || getFieldValue('new_password') === value
                      ? Promise.resolve()
                      : Promise.reject(new Error('两次输入的密码不一致')),
                }),
              ]}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" loading={saving} onClick={() => void changePassword()}>修改密码</Button>
          </Form>
        </Card>
      )}
    </Space>
  )
}
