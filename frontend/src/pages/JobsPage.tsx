import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  App, Button, Card, Dropdown, Flex, Input, Progress, Select, Space, Switch, Table, Tag, Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  DeleteOutlined, DownloadOutlined, MoreOutlined, PauseCircleOutlined, PlayCircleOutlined,
  PlusOutlined, ReloadOutlined, StopOutlined,
} from '@ant-design/icons'
import { api, downloadUrl } from '../api'
import type { Job, JobStatus } from '../api'
import JobStatusTag from '../components/JobStatusTag'
import { useAuth } from '../hooks/useAuth'
import { usePolling } from '../hooks/usePolling'
import { ACTIVE_STATUSES, STATUS_LABEL, formatDateTime, formatDuration, formatNumber } from '../utils'

const STATUS_OPTIONS = (Object.keys(STATUS_LABEL) as JobStatus[]).map((s) => ({
  value: s, label: STATUS_LABEL[s],
}))

export default function JobsPage() {
  const navigate = useNavigate()
  const { modal, message } = App.useApp()
  const { isAdmin } = useAuth()

  const location = useLocation()
  const [data, setData] = useState<Job[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)

  // 页码与筛选条件放在网址参数里：从详情页返回、浏览器后退、刷新页面都能回到原来那一页
  const [searchParams, setSearchParams] = useSearchParams()
  const page = Number(searchParams.get('page')) || 1
  const pageSize = Number(searchParams.get('size')) || 20
  const statusParam = searchParams.get('status') ?? ''
  const statuses = useMemo(
    () => (statusParam ? statusParam.split(',') as JobStatus[] : []), [statusParam],
  )
  const keyword = searchParams.get('q') ?? ''
  const mine = searchParams.get('all') !== '1'

  /** 改网址参数；值为 null/空串即删掉该项。除了翻页，改其他条件都回到第一页 */
  const updateQuery = useCallback((changes: Record<string, string | number | null>) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (!('page' in changes)) next.delete('page')
      for (const [key, value] of Object.entries(changes)) {
        if (value === null || value === '') next.delete(key)
        else next.set(key, String(value))
      }
      return next
    }, { replace: true })  // 翻页不往浏览器历史里塞记录
  }, [setSearchParams])

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const res = await api.jobs({
        page, page_size: pageSize,
        status: statuses.length ? statuses.join(',') : undefined,
        keyword: keyword || undefined,
        mine: isAdmin ? mine : true,
      })
      // 回来时这一页可能已经空了（比如在详情页删掉了最后一页唯一的任务），退到最后一页
      if (!res.items.length && page > 1 && res.total > 0) {
        const last = Math.ceil(res.total / pageSize)
        updateQuery({ page: last > 1 ? last : null })
        return
      }
      setData(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, statuses, keyword, mine, isAdmin, updateQuery])

  /** 进详情时带上列表的网址参数，详情页的「返回」按它回到原来那一页 */
  const openJob = (id: string) => navigate(`/jobs/${id}`, { state: { from: location.search } })

  useEffect(() => { void load(true) }, [load])

  // 有任务在跑时才轮询，跑完自动停下来
  const hasActive = useMemo(() => data.some((j) => ACTIVE_STATUSES.includes(j.status)), [data])
  usePolling(() => void load(), hasActive ? 3000 : null)

  const act = async (fn: () => Promise<unknown>, okText: string) => {
    try {
      await fn()
      message.success(okText)
      await load()
    } catch {
      // 拦截器已提示
    }
  }

  const confirmDelete = (job: Job) => {
    modal.confirm({
      title: `删除任务「${job.name}」？`,
      content: '任务记录、上传的输入文件与已生成的结果都会被永久删除，此操作不可撤销。',
      okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: () => act(() => api.deleteJob(job.id), '已删除'),
    })
  }

  const columns: ColumnsType<Job> = [
    {
      title: '任务名称', dataIndex: 'name', width: 220, ellipsis: true,
      render: (name: string, job) => (
        <a onClick={() => openJob(job.id)}>{name}</a>
      ),
    },
    ...(isAdmin && !mine
      ? [{ title: '提交者', dataIndex: 'username', width: 120 } as ColumnsType<Job>[number]]
      : []),
    {
      title: '模型', width: 200, ellipsis: true,
      render: (_, job) => (
        <Space size={4}>
          <Tag color={job.model_source === 'shared' ? 'blue' : 'default'} style={{ marginInlineEnd: 0 }}>
            {job.model_source === 'shared' ? '公用' : '个人'}
          </Tag>
          <Typography.Text ellipsis>{job.model_display_name ?? '—'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态', dataIndex: 'status', width: 130,
      render: (status: JobStatus, job) => (
        <Space size={4}>
          <JobStatusTag status={status} />
          {job.files_purged_at ? (
            <Tooltip title="文件已超过保留期被清理，任务记录仍保留">
              <Tag>已清理</Tag>
            </Tooltip>
          ) : null}
          {job.queue_position ? (
            <Typography.Text type="secondary">第 {job.queue_position} 位</Typography.Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: '进度', width: 200,
      render: (_, job) => (
        <Tooltip title={`成功 ${formatNumber(job.completed_items)} / 失败 ${formatNumber(job.failed_items)} / 共 ${formatNumber(job.total_items)}`}>
          <Progress
            percent={job.progress}
            size="small"
            status={job.status === 'running' ? 'active' : job.failed_items ? 'exception' : undefined}
          />
        </Tooltip>
      ),
    },
    { title: '耗时', width: 110,
      render: (_, job) => formatDuration(job.started_at, job.finished_at) },
    { title: '创建时间', dataIndex: 'created_at', width: 170, render: formatDateTime },
    {
      title: '操作', width: 120, fixed: 'right',
      render: (_, job) => {
        const canPause = job.status === 'running' || job.status === 'queued'
        const canResume = ['paused', 'failed', 'canceled'].includes(job.status)
        const canCancel = !['succeeded', 'completed', 'failed', 'canceled'].includes(job.status)
        const finished = ['succeeded', 'completed', 'canceled'].includes(job.status)
        const purged = !!job.files_purged_at
        // 结果文件在任务跑动时一直被追加写入，跑完之前不给下载
        const canDownload = finished && !purged && (job.completed_items > 0 || job.failed_items > 0)
        return (
          <Dropdown
            trigger={['click']}
            menu={{
              items: [
                { key: 'detail', label: '查看详情' },
                ...(canDownload
                  ? [{ key: 'download', icon: <DownloadOutlined />, label: '下载结果 (JSONL)' }]
                  : []),
                ...(canPause ? [{ key: 'pause', icon: <PauseCircleOutlined />, label: '暂停' }] : []),
                ...(canResume && !purged
                  ? [{ key: 'resume', icon: <PlayCircleOutlined />, label: '恢复' }] : []),
                ...(job.failed_items > 0 && finished && !purged
                  ? [{ key: 'retry', icon: <ReloadOutlined />, label: '重试失败条目' }] : []),
                ...(canCancel ? [{ key: 'cancel', icon: <StopOutlined />, label: '取消', danger: true }] : []),
                ...(!canCancel ? [{ key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true }] : []),
              ],
              onClick: ({ key, domEvent }) => {
                domEvent.stopPropagation()
                if (key === 'detail') openJob(job.id)
                if (key === 'download') window.open(downloadUrl(`/jobs/${job.id}/download?fmt=raw`), '_blank')
                if (key === 'pause') void act(() => api.pauseJob(job.id), '已请求暂停')
                if (key === 'resume') void act(() => api.resumeJob(job.id), '已重新入队')
                if (key === 'retry') void act(() => api.retryFailed(job.id), '已重新入队失败条目')
                if (key === 'cancel') void act(() => api.cancelJob(job.id), '已请求取消')
                if (key === 'delete') confirmDelete(job)
              },
            }}
          >
            <Button type="text" icon={<MoreOutlined />} />
          </Dropdown>
        )
      },
    },
  ]

  return (
    <Card
      title="任务列表"
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load(true)}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/jobs/new')}>
            新建任务
          </Button>
        </Space>
      }
    >
      <Flex gap={12} wrap style={{ marginBottom: 16 }}>
        <Input.Search
          allowClear placeholder="搜索任务名称" style={{ width: 240 }}
          defaultValue={keyword}
          onSearch={(v) => updateQuery({ q: v.trim() })}
        />
        <Select
          mode="multiple" allowClear placeholder="按状态筛选" style={{ minWidth: 220 }}
          options={STATUS_OPTIONS} value={statuses}
          onChange={(v: JobStatus[]) => updateQuery({ status: v.join(',') })}
        />
        {isAdmin && (
          <Space>
            <Switch checked={!mine} onChange={(v) => updateQuery({ all: v ? '1' : null })} />
            <Typography.Text type="secondary">查看全部用户的任务</Typography.Text>
          </Space>
        )}
      </Flex>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={data}
        scroll={{ x: 1100 }}
        pagination={{
          current: page, pageSize, total, showSizeChanger: true,
          showTotal: (t) => `共 ${formatNumber(t)} 个任务`,
          // 默认值不写进网址，保持地址干净
          onChange: (p, ps) => updateQuery({ page: p > 1 ? p : null, size: ps !== 20 ? ps : null }),
        }}
      />
    </Card>
  )
}
