import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { App, Button, Card, Input, Select, Space, Statistic, Table, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { BillingRecord, BillingRecordList } from '../api'
import { billingColumns } from '../components/BillingRecords'

const PAGE_SIZE = 50

export default function AdminBillingPage() {
  const { message } = App.useApp()
  const [data, setData] = useState<BillingRecordList | null>(null)
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState<string | undefined>()
  const [username, setUsername] = useState('')
  const [jobId, setJobId] = useState('')
  const [page, setPage] = useState(1)
  const [retrying, setRetrying] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.billingRecords({
        status, username: username.trim() || undefined, job_id: jobId.trim() || undefined,
        page, page_size: PAGE_SIZE,
      }))
    } finally {
      setLoading(false)
    }
  }, [status, username, jobId, page])

  useEffect(() => { void load() }, [load])

  const retry = async (record: BillingRecord) => {
    setRetrying(record.id)
    try {
      const res = await api.retryBillingRecord(record.id)
      if (res.status === 'succeeded') message.success('扣费成功')
      else message.warning(res.error_hint || res.error || '仍未扣上，已留待重试')
      await load()
    } finally {
      setRetrying(null)
    }
  }

  const columns = billingColumns([
    { title: '用户', dataIndex: 'username', width: 110 },
    {
      title: '任务', key: 'job', width: 180, ellipsis: true,
      render: (_, r) => (r.job_id
        ? <Link to={`/jobs/${r.job_id}`}>{r.job_name ?? r.job_id}</Link>
        : <Typography.Text type="secondary">{r.note ?? '—'}</Typography.Text>),
    },
  ])
  columns.push({
    title: '操作', key: 'op', width: 90, fixed: 'right',
    render: (_, r) => (r.status === 'succeeded' ? null : (
      <Button size="small" loading={retrying === r.id} onClick={() => void retry(r)}>重发</Button>
    )),
  })

  const counts = data?.counts ?? {}
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space size={48} wrap>
          <Statistic title="已扣费" value={counts.succeeded ?? 0} suffix="笔" />
          <Statistic title="待扣费" value={counts.pending ?? 0} suffix="笔" />
          <Statistic
            title="扣费失败" value={counts.failed ?? 0} suffix="笔"
            valueStyle={counts.failed ? { color: 'var(--ant-color-error, #ff4d4f)' } : undefined}
          />
        </Space>
      </Card>

      <Card
        title="计费记录"
        extra={
          <Space wrap>
            <Select
              allowClear placeholder="全部状态" style={{ width: 130 }} value={status}
              onChange={(v) => { setStatus(v); setPage(1) }}
              options={[
                { value: 'succeeded', label: '已扣费' },
                { value: 'pending', label: '待扣费' },
                { value: 'failed', label: '扣费失败' },
              ]}
            />
            <Input.Search
              allowClear placeholder="用户名" style={{ width: 160 }}
              onSearch={(v) => { setUsername(v); setPage(1) }}
            />
            <Input.Search
              allowClear placeholder="任务 ID" style={{ width: 200 }} className="mono"
              onSearch={(v) => { setJobId(v); setPage(1) }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
          </Space>
        }
      >
        <Table
          rowKey="id" size="small" loading={loading} columns={columns} dataSource={data?.items ?? []}
          scroll={{ x: 1400 }}
          pagination={{
            current: page, pageSize: PAGE_SIZE, total: data?.total ?? 0, showSizeChanger: false,
            onChange: setPage, showTotal: (t) => `共 ${t} 笔`,
          }}
        />
      </Card>
    </Space>
  )
}
