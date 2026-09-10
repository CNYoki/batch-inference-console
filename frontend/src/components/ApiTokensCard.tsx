import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Typography,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../api'
import type { ApiToken, ApiTokenCreated } from '../api'
import { formatDateTime } from '../utils'

const EXPIRY_OPTIONS = [
  { value: 30, label: '30 天' },
  { value: 90, label: '90 天' },
  { value: 180, label: '180 天' },
  { value: 365, label: '1 年' },
  { value: 0, label: '永不过期' },
]

function isExpired(t: ApiToken): boolean {
  return !!t.expires_at && new Date(t.expires_at).getTime() <= Date.now()
}

/** 写配置文件的一行命令：token 不经过 shell 历史以外的任何地方，也不用贴进 Claude 对话 */
function setupCommand(token: string): string {
  const cfg = JSON.stringify({ url: window.location.origin, token })
  return `mkdir -p ~/.config/bic && (umask 077; echo '${cfg}' > ~/.config/bic/config.json)`
}

export default function ApiTokensCard() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [rows, setRows] = useState<ApiToken[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [formOpen, setFormOpen] = useState(false)
  const [created, setCreated] = useState<ApiTokenCreated | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await api.apiTokens())
    } catch { /* 拦截器已提示 */ } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const submit = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      const res = await api.createApiToken(values.name.trim(), values.expires_in_days || null)
      setFormOpen(false)
      form.resetFields()
      setCreated(res)
      await load()
    } catch { /* 拦截器已提示 */ } finally {
      setCreating(false)
    }
  }

  const remove = async (t: ApiToken) => {
    try {
      await api.deleteApiToken(t.id)
      message.success(`已吊销「${t.name}」`)
      await load()
    } catch { /* 拦截器已提示 */ }
  }

  const columns: ColumnsType<ApiToken> = [
    { title: '名称', dataIndex: 'name' },
    {
      title: 'Token', dataIndex: 'token_prefix',
      render: (v: string) => <span className="mono">{v}…</span>,
    },
    { title: '创建时间', dataIndex: 'created_at', render: (v: string) => formatDateTime(v) },
    {
      title: '过期时间', dataIndex: 'expires_at',
      render: (v: string | null, t) => (
        isExpired(t) ? <Tag color="red">已过期</Tag> : (v ? formatDateTime(v) : <Tag>永不过期</Tag>)
      ),
    },
    {
      title: '最近使用', dataIndex: 'last_used_at',
      render: (v: string | null) => (v ? formatDateTime(v) : '从未使用'),
    },
    {
      title: '', key: 'actions', width: 80,
      render: (_, t) => (
        <Popconfirm
          title="吊销这个 token？" description="使用它的命令行和 Skills 会立即失去访问权限"
          okText="吊销" okButtonProps={{ danger: true }} onConfirm={() => void remove(t)}
        >
          <Button size="small" danger type="link">吊销</Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Card
      title="API Token"
      extra={<Button icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>新建 token</Button>}
    >
      <Typography.Paragraph type="secondary">
        给命令行工具 <span className="mono">bic</span> 和 Claude Code 的 batch-inference 插件使用，
        可以在本地完成数据预处理、上传和提交任务。token 拥有你账号的全部任务权限，请像密码一样保管。
      </Typography.Paragraph>
      <Table
        rowKey="id" size="small" loading={loading} columns={columns} dataSource={rows}
        pagination={false} scroll={{ x: 720 }} locale={{ emptyText: '还没有 token' }}
      />

      <Modal
        title="新建 API Token" open={formOpen} confirmLoading={creating}
        onOk={() => void submit()} onCancel={() => setFormOpen(false)} okText="生成" destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ expires_in_days: 90 }} preserve={false}>
          <Form.Item
            name="name" label="名称" extra="用来区分在哪台机器、给什么用，例如「笔记本 Claude Code」"
            rules={[{ required: true, whitespace: true, message: '请填写名称' }]}
          >
            <Input maxLength={128} />
          </Form.Item>
          <Form.Item name="expires_in_days" label="有效期">
            <Select options={EXPIRY_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="token 已生成" open={!!created} onCancel={() => setCreated(null)} width={680}
        footer={<Button type="primary" onClick={() => setCreated(null)}>我已保存</Button>}
      >
        {created && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert type="warning" showIcon message="明文只显示这一次，关闭后无法再查看。丢了就吊销重建。" />
            <Typography.Paragraph copyable={{ text: created.token }} className="mono" style={{ marginBottom: 0 }}>
              {created.token}
            </Typography.Paragraph>
            <Typography.Text strong>在本机终端执行下面这行即可完成配置（macOS / Linux）：</Typography.Text>
            <Typography.Paragraph
              copyable={{ text: setupCommand(created.token) }} code
              style={{ wordBreak: 'break-all', marginBottom: 0 }}
            >
              {setupCommand(created.token)}
            </Typography.Paragraph>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              Windows 或临时使用：设置环境变量 <span className="mono">BIC_URL={window.location.origin}</span>{' '}
              与 <span className="mono">BIC_TOKEN</span>。请在自己的终端里执行，不要把 token 粘贴进和 Claude 的对话。
            </Typography.Text>
          </Space>
        )}
      </Modal>
    </Card>
  )
}
