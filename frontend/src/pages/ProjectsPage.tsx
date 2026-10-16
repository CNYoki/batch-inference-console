import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { App, Button, Card, Form, Input, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  DatabaseOutlined, DeleteOutlined, EditOutlined, MessageOutlined, PlusOutlined,
} from '@ant-design/icons'
import { api } from '../api'
import type { Project } from '../api'
import { formatDateTime, formatNumber } from '../utils'

type FormValues = { name: string; description?: string }

export default function ProjectsPage() {
  const { modal, message } = App.useApp()
  const navigate = useNavigate()
  const [form] = Form.useForm<FormValues>()
  const [rows, setRows] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Project | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await api.projects())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openModal = (record?: Project) => {
    setEditing(record ?? null)
    form.resetFields()
    form.setFieldsValue({ name: record?.name ?? '', description: record?.description ?? '' })
    setOpen(true)
  }

  const save = async () => {
    const values = await form.validateFields()
    const payload = { name: values.name.trim(), description: values.description?.trim() || null }
    setSaving(true)
    try {
      if (editing) {
        await api.updateProject(editing.id, payload)
        message.success('已保存')
      } else {
        await api.createProject(payload)
        message.success('已创建')
      }
      setOpen(false)
      await load()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  const columns: ColumnsType<Project> = [
    {
      title: '项目', width: 260,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <a onClick={() => navigate(`/jobs?project=${r.id}`)}><Typography.Text strong>{r.name}</Typography.Text></a>
            {r.is_default && <Tag color="blue">默认</Tag>}
          </Space>
          {r.description && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{r.description}</Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '任务', width: 180,
      render: (_, r) => (
        <Space size={4}>
          <a onClick={() => navigate(`/jobs?project=${r.id}`)}>{formatNumber(r.job_count)} 个</a>
          {r.active_job_count > 0 && <Tag color="processing">{r.active_job_count} 个进行中</Tag>}
        </Space>
      ),
    },
    {
      title: 'Prompt', width: 120,
      render: (_, r) => (
        <a onClick={() => navigate(`/tools/prompts?project=${r.id}`)}>{formatNumber(r.prompt_count)} 个</a>
      ),
    },
    { title: '创建时间', width: 170, render: (_, r) => formatDateTime(r.created_at) },
    {
      title: '操作', width: 330, fixed: 'right',
      render: (_, r) => (
        <Space size={4}>
          <Button size="small" type="primary" ghost icon={<PlusOutlined />}
            onClick={() => navigate(`/jobs/new?project=${r.id}`)}>在此项目新建任务</Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openModal(r)}>编辑</Button>
          {r.is_default ? (
            <Tooltip title="默认项目不能删除">
              <Button size="small" danger icon={<DeleteOutlined />} disabled />
            </Tooltip>
          ) : (
            <Button size="small" danger icon={<DeleteOutlined />} onClick={() => modal.confirm({
              title: `删除项目「${r.name}」？`,
              content: '项目里的任务不会被删除，会挪回默认项目；其中的 Prompt 变成「未归类」。',
              okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
              onOk: async () => { await api.deleteProject(r.id); message.success('已删除'); await load() },
            })} />
          )}
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="项目管理"
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>新建项目</Button>}
    >
      <Typography.Paragraph type="secondary">
        用项目把同一件事的任务和 Prompt 归到一起，方便在 <DatabaseOutlined /> 任务列表和{' '}
        <MessageOutlined /> 我的 Prompt 里按项目筛选。每个任务都属于一个项目，没有指定的放进默认项目。
        项目只对你自己可见，删除项目不会删除其中的内容。
      </Typography.Paragraph>
      <Table rowKey="id" loading={loading} columns={columns} dataSource={rows} scroll={{ x: 1000 }}
        pagination={rows.length > 20 ? { pageSize: 20 } : false}
        locale={{ emptyText: '还没有项目，点右上角新建一个' }} />

      <Modal
        title={editing ? `编辑项目：${editing.name}` : '新建项目'}
        open={open} onCancel={() => setOpen(false)} onOk={() => void save()}
        confirmLoading={saving} okText="保存" cancelText="取消" destroyOnClose={false}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称"
            rules={[{ required: true, whitespace: true, message: '请填写项目名称' }]}>
            <Input maxLength={128} placeholder="例：某科研任务" />
          </Form.Item>
          <Form.Item name="description" label="说明（可选）">
            <Input.TextArea maxLength={1024} rows={3} placeholder="这个项目做什么" />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
