import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, App, Button, Card, Empty, Progress, Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowDownOutlined, ArrowUpOutlined, HolderOutlined, PushpinFilled, PushpinOutlined,
  ReloadOutlined, VerticalAlignBottomOutlined, VerticalAlignTopOutlined,
} from '@ant-design/icons'
import { api } from '../api'
import type { Job, QueueView } from '../api'
import JobStatusTag from '../components/JobStatusTag'
import { usePolling } from '../hooks/usePolling'
import { formatDateTime, formatNumber } from '../utils'

function modelCell(job: Job) {
  return (
    <Space size={4}>
      <Tag color={job.model_source === 'shared' ? 'blue' : 'default'} style={{ marginInlineEnd: 0 }}>
        {job.model_source === 'shared' ? '公用' : '个人'}
      </Tag>
      <Typography.Text ellipsis>{job.model_display_name ?? '—'}</Typography.Text>
    </Space>
  )
}

/** 把 list 里 from 位置的元素挪到 to 位置 */
function moved<T>(list: T[], from: number, to: number): T[] {
  const next = [...list]
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  return next
}

export default function AdminQueuePage() {
  const navigate = useNavigate()
  const { modal, message } = App.useApp()
  const [view, setView] = useState<QueueView | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const dragFrom = useRef<number | null>(null)

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      setView(await api.queue())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(true) }, [load])
  // 排序请求进行中先别刷新，免得列表在手底下跳动
  usePolling(() => void load(), saving ? null : 5000)

  const queued = view?.queued ?? []

  const reorder = async (from: number, to: number) => {
    if (from === to || to < 0 || to >= queued.length) return
    const next = moved(queued, from, to)
    // 先在界面上挪过去，失败了再刷新回来
    setView((v) => (v ? { ...v, queued: next } : v))
    setSaving(true)
    try {
      setView(await api.reorderQueue(next.map((j) => j.id)))
    } catch {
      await load()
    } finally {
      setSaving(false)
    }
  }

  const act = async (fn: () => Promise<unknown>, okText: string) => {
    try {
      await fn()
      message.success(okText)
      await load()
    } catch { /* 拦截器已提示 */ }
  }

  const confirmPin = (job: Job) => modal.confirm({
    title: `置顶任务「${job.name}」？`,
    content: '置顶后它排到队首；同一模型上正在运行的其他任务会暂停下来回到队列，'
      + '直到它完成、失败或被取消后才继续（已完成的条目不会重跑）。',
    okText: '置顶', cancelText: '取消',
    onOk: () => act(() => api.pinJob(job.id), '已置顶'),
  })

  const nameCell = (job: Job) => (
    <Space direction="vertical" size={0}>
      <a onClick={() => navigate(`/jobs/${job.id}`)}>{job.name}</a>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {job.username ?? '—'}{job.project_name ? ` · ${job.project_name}` : ''}
      </Typography.Text>
    </Space>
  )

  const pinnedColumns: ColumnsType<Job> = [
    { title: '任务', width: 260, render: (_, job) => nameCell(job) },
    { title: '模型', width: 200, ellipsis: true, render: (_, job) => modelCell(job) },
    {
      title: '状态', width: 180,
      render: (_, job) => (
        <Space size={4}>
          <JobStatusTag status={job.status} />
          {job.status === 'paused' && (
            <Tooltip title="暂停期间不占用模型，恢复后重新抢占">
              <Typography.Text type="secondary">不占模型</Typography.Text>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '进度', width: 180,
      render: (_, job) => <Progress percent={job.progress} size="small"
        status={job.status === 'running' ? 'active' : undefined} />,
    },
    { title: '置顶时间', width: 170, render: (_, job) => formatDateTime(job.pinned_at) },
    {
      title: '操作', width: 120, fixed: 'right',
      render: (_, job) => (
        <Button size="small" icon={<PushpinOutlined />}
          onClick={() => void act(() => api.unpinJob(job.id), '已取消置顶')}>取消置顶</Button>
      ),
    },
  ]

  const queuedColumns: ColumnsType<Job> = [
    {
      title: '', width: 36,
      render: () => (
        <Tooltip title="拖动调整顺序"><HolderOutlined style={{ cursor: 'grab', color: '#999' }} /></Tooltip>
      ),
    },
    { title: '位置', width: 70, render: (_, job) => job.queue_position ?? '—' },
    { title: '任务', width: 260, render: (_, job) => nameCell(job) },
    { title: '模型', width: 200, ellipsis: true, render: (_, job) => modelCell(job) },
    { title: '条目', width: 100, render: (_, job) => formatNumber(job.total_items) },
    { title: '优先级', dataIndex: 'priority', width: 80 },
    { title: '入队时间', width: 170, render: (_, job) => formatDateTime(job.queued_at) },
    {
      title: '操作', width: 230, fixed: 'right',
      render: (_, job, index) => (
        <Space size={2}>
          <Tooltip title="移到队首">
            <Button size="small" type="text" icon={<VerticalAlignTopOutlined />}
              disabled={index === 0 || saving} onClick={() => void reorder(index, 0)} />
          </Tooltip>
          <Tooltip title="上移">
            <Button size="small" type="text" icon={<ArrowUpOutlined />}
              disabled={index === 0 || saving} onClick={() => void reorder(index, index - 1)} />
          </Tooltip>
          <Tooltip title="下移">
            <Button size="small" type="text" icon={<ArrowDownOutlined />}
              disabled={index === queued.length - 1 || saving}
              onClick={() => void reorder(index, index + 1)} />
          </Tooltip>
          <Tooltip title="移到队尾">
            <Button size="small" type="text" icon={<VerticalAlignBottomOutlined />}
              disabled={index === queued.length - 1 || saving}
              onClick={() => void reorder(index, queued.length - 1)} />
          </Tooltip>
          <Button size="small" icon={<PushpinOutlined />} onClick={() => confirmPin(job)}>置顶</Button>
        </Space>
      ),
    },
  ]

  const truncated = view ? view.total_queued > queued.length + view.pinned.filter((j) => j.status === 'queued').length : false

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info" showIcon
        message="队列规则"
        description={
          <ul style={{ paddingLeft: 18, margin: 0 }}>
            <li>
              <PushpinFilled style={{ color: '#fa541c' }} /> 置顶任务排在所有任务之前。
              它排队或运行期间，同一模型上正在运行的其他任务会暂停下来回到队列（保持原来的先后），
              直到置顶任务完成、失败或被取消后才继续；已完成的条目不会重跑。置顶任务被暂停时不占用模型。
            </li>
            <li>
              下方排队序列可以拖动或用按钮调整先后。worker 按这个顺序领取，
              但单模型任务数上限、置顶占用会让排在后面的其他模型任务先跑。
            </li>
          </ul>
        }
      />

      <Card
        title={<Space><PushpinFilled style={{ color: '#fa541c' }} />置顶任务</Space>}
        extra={<Button icon={<ReloadOutlined />} onClick={() => void load(true)}>刷新</Button>}
      >
        <Table rowKey="id" size="small" loading={loading} columns={pinnedColumns}
          dataSource={view?.pinned ?? []} pagination={false} scroll={{ x: 1110 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有置顶任务" /> }} />
      </Card>

      <Card title={`排队序列（共 ${formatNumber(view?.total_queued ?? 0)} 个）`}>
        {truncated && (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }}
            message={`只显示了前 ${queued.length} 个排队任务，调整顺序只影响这些任务彼此之间的先后`} />
        )}
        <Table
          rowKey="id" size="small" loading={loading} columns={queuedColumns} dataSource={queued}
          pagination={false} scroll={{ x: 1150 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="队列是空的" /> }}
          onRow={(_, index) => ({
            draggable: !saving,
            onDragStart: () => { dragFrom.current = index ?? null },
            onDragOver: (e) => e.preventDefault(),
            onDrop: (e) => {
              e.preventDefault()
              const from = dragFrom.current
              dragFrom.current = null
              if (from !== null && index !== undefined) void reorder(from, index)
            },
          })}
        />
      </Card>
    </Space>
  )
}
