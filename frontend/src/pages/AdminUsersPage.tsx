import { useCallback, useEffect, useState } from 'react'
import {
  App, Button, Card, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Switch, Table, Tag, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { User } from '../api'
import { useAuth } from '../hooks/useAuth'
import { formatBytes, formatDateTime } from '../utils'

export default function AdminUsersPage() {
  const { modal, message } = App.useApp()
  const { user: me } = useAuth()
  const [form] = Form.useForm()
  const [rows, setRows] = useState<User[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.users({ page, page_size: 20, keyword: keyword || undefined })
      setRows(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page, keyword])

  useEffect(() => { void load() }, [load])

  const openModal = (record?: User) => {
    setEditing(record ?? null)
    form.resetFields()
    if (record) {
      form.setFieldsValue({ ...record, password: '' })
    } else {
      form.setFieldsValue({ role: 'user', max_concurrent_jobs: 0, max_storage_mb: 0, notify_email: true })
    }
    setOpen(true)
  }

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (editing) {
        const payload: Record<string, unknown> = {
          email: values.email, display_name: values.display_name, role: values.role,
          is_active: values.is_active, max_concurrent_jobs: values.max_concurrent_jobs,
          max_storage_mb: values.max_storage_mb, notify_email: values.notify_email,
        }
        if (values.password) payload.password = values.password  // 留空表示不改密码
        await api.updateUser(editing.id, payload)
        message.success('已保存')
      } else {
        await api.createUser(values)
        message.success('已创建')
      }
      setOpen(false)
      await load()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  const columns: ColumnsType<User> = [
    {
      title: '用户', width: 220,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Typography.Text strong>{r.display_name || r.username}</Typography.Text>
            {r.role === 'admin' && <Tag color="gold">管理员</Tag>}
            {!r.is_active && <Tag color="red">已禁用</Tag>}
            {r.id === me?.id && <Tag>我</Tag>}
          </Space>
          <Typography.Text type="secondary" className="mono">{r.username}</Typography.Text>
        </Space>
      ),
    },
    { title: '邮箱', dataIndex: 'email', width: 220, render: (v: string | null) => v || '—' },
    {
      title: '登录方式', dataIndex: 'auth_source', width: 110,
      render: (v: string) => v === 'oidc' ? <Tag color="blue">统一登录</Tag> : <Tag>本地账号</Tag>,
    },
    {
      title: '并发任务上限', dataIndex: 'max_concurrent_jobs', width: 110,
      render: (v: number) => (v ? v : '不限'),
    },
    {
      title: '存储上限', dataIndex: 'max_storage_mb', width: 110,
      render: (v: number) => (v ? formatBytes(v * 1024 * 1024) : '按全局默认'),
    },
    {
      title: '邮件通知', dataIndex: 'notify_email', width: 100,
      render: (v: boolean, r) => (v
        ? (r.email ? <Tag color="green">开启</Tag> : <Tag color="orange">开启(无邮箱)</Tag>)
        : <Tag>关闭</Tag>),
    },
    { title: '最近登录', dataIndex: 'last_login_at', width: 170, render: formatDateTime },
    { title: '创建时间', dataIndex: 'created_at', width: 170, render: formatDateTime },
    {
      title: '操作', width: 130, fixed: 'right',
      render: (_, r) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openModal(r)}>编辑</Button>
          <Button size="small" danger icon={<DeleteOutlined />} disabled={r.id === me?.id}
            onClick={() => modal.confirm({
              title: `删除用户「${r.username}」？`,
              content: '该用户的全部任务记录与结果文件也会被删除。',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: async () => { await api.deleteUser(r.id); message.success('已删除'); await load() },
            })} />
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="用户管理"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>新增本地账号</Button>}
    >
      <Input.Search allowClear placeholder="搜索用户名 / 邮箱" style={{ width: 260, marginBottom: 16 }}
        onSearch={(v) => { setKeyword(v); setPage(1) }} />
      <Table rowKey="id" loading={loading} columns={columns} dataSource={rows} scroll={{ x: 1000 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} />

      <Modal
        title={editing ? `编辑用户：${editing.username}` : '新增本地账号'}
        open={open} onCancel={() => setOpen(false)} onOk={() => void save()}
        confirmLoading={saving} okText="保存" cancelText="取消" destroyOnClose width={640}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="username" label="用户名" rules={[{ required: !editing, message: '请填写用户名' }]}>
            <Input disabled={!!editing} className="mono" />
          </Form.Item>
          <Form.Item name="password" label={editing ? '重置密码（留空表示不修改）' : '初始密码'}
            rules={editing ? [{ min: 8, message: '至少 8 位' }] : [{ required: true, min: 8, message: '至少 8 位' }]}>
            <Input.Password placeholder={editing ? '不修改请留空' : '至少 8 位'}
              disabled={!!editing && editing.auth_source !== 'local'} />
          </Form.Item>
          <Form.Item name="display_name" label="显示名"><Input /></Form.Item>
          <Form.Item name="email" label="邮箱"><Input type="email" /></Form.Item>
          <Row gutter={16}>
            <Col xs={24} sm={8}>
              <Form.Item name="role" label="角色">
                <Select options={[{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }]} />
              </Form.Item>
            </Col>
            <Col xs={12} sm={8}>
              <Form.Item name="max_concurrent_jobs" label="并发任务上限" extra="0 表示不限">
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} sm={8}>
              <Form.Item name="max_storage_mb" label="存储上限" extra="0 = 用全局默认值">
                <InputNumber min={0} style={{ width: '100%' }} addonAfter="MB" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={12} sm={8}>
              <Form.Item name="notify_email" label="邮件通知" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
            {editing && (
              <Col xs={12} sm={8}>
                <Form.Item name="is_active" label="启用" valuePropName="checked"><Switch /></Form.Item>
              </Col>
            )}
          </Row>
        </Form>
      </Modal>
    </Card>
  )
}
