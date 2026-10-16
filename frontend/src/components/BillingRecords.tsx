import { useCallback, useEffect, useState } from 'react'
import { Alert, App, Button, Empty, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { api } from '../api'
import type { BillingRecord, BillingStatus, Job, JobBilling } from '../api'
import { formatDateTime, formatNumber } from '../utils'

const STATUS: Record<BillingStatus, { label: string; color: string }> = {
  succeeded: { label: '已扣费', color: 'success' },
  pending: { label: '待扣费', color: 'processing' },
  failed: { label: '扣费失败', color: 'error' },
}

export function BillingStatusTag({ record }: { record: BillingRecord }) {
  const s = STATUS[record.status]
  const tag = <Tag color={s.color}>{s.label}{record.duplicate ? '（重复请求）' : ''}</Tag>
  if (record.status === 'succeeded' || !(record.error_hint || record.error)) return tag
  return <Tooltip title={record.error_hint || record.error}>{tag}</Tooltip>
}

/** 计费记录表的公共列；extra 插在状态列之前 */
export function billingColumns(extra: ColumnsType<BillingRecord> = []): ColumnsType<BillingRecord> {
  return [
    {
      title: '时间', dataIndex: 'created_at', width: 170,
      render: (v: string) => formatDateTime(v),
    },
    ...extra,
    { title: '模型', dataIndex: 'model_name', width: 160, render: (v: string) => <span className="mono">{v}</span> },
    {
      title: '用量（入 / 出 / 缓存）', key: 'usage', width: 200,
      render: (_, r) => `${formatNumber(r.prompt_tokens)} / ${formatNumber(r.completion_tokens)}`
        + ` / ${formatNumber(r.cached_tokens)}`,
    },
    {
      title: '扣除额度', dataIndex: 'quota', width: 110,
      render: (v: number | null, r) => (v == null ? '—' : (
        <Tooltip title={r.billing_source === 'subscription' ? '订阅额度' : '钱包余额'}>
          {formatNumber(v)}
        </Tooltip>
      )),
    },
    { title: '状态', key: 'status', width: 120, render: (_, r) => <BillingStatusTag record={r} /> },
    {
      title: '说明', key: 'error', ellipsis: true,
      render: (_, r) => (r.status === 'succeeded'
        ? <Typography.Text type="secondary">{formatDateTime(r.billed_at)}</Typography.Text>
        : (r.error_hint || r.error || (r.attempts ? `已尝试 ${r.attempts} 次` : '等待发送'))),
    },
    {
      title: '请求 ID', dataIndex: 'request_id', width: 150, ellipsis: true,
      render: (v: string) => (
        <Typography.Text className="mono" copyable={{ text: v }} style={{ fontSize: 12 }}>
          {v.length > 16 ? `…${v.slice(-12)}` : v}
        </Typography.Text>
      ),
    },
  ]
}

/** 任务详情里的「计费」页：小结 + 最近的计费记录 + 重试 */
export function JobBillingPanel({ job, onChanged }: { job: Job; onChanged?: () => void }) {
  const { message } = App.useApp()
  const [data, setData] = useState<JobBilling | null>(null)
  const [retrying, setRetrying] = useState(false)

  const load = useCallback(async () => {
    setData(await api.jobBilling(job.id))
  }, [job.id])

  // 用量变了（运行中定期扣费）就跟着刷新
  useEffect(() => { void load().catch(() => undefined) }, [load, job.status, job.prompt_tokens])

  if (!data) return <Empty description="加载中…" />
  if (!data.enabled && !data.records.length) {
    return <Empty description="该任务不走个人网关独立计费" />
  }

  const retry = async () => {
    setRetrying(true)
    try {
      const res = await api.retryJobBilling(job.id)
      setData(res)
      if (res.failed.count) message.warning(res.message || '仍有扣费失败的记录')
      else message.success(res.message || '已重发')
      onChanged?.()
    } finally {
      setRetrying(false)
    }
  }

  const unsettled = data.pending.count + data.failed.count
  // 错误说明是按暂停中的任务写的（点「继续」）；已结束的任务没有「继续」，改指向这里的按钮
  const hint = data.records.find((r) => r.status === 'failed')?.error_hint
  const failedHint = job.status === 'paused' ? hint : hint?.replace(/点「继续」(即可补扣并接着跑)?/, '点右侧「重新扣费」')
  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {data.failed.count > 0 && (
        <Alert
          type="error" showIcon
          message={`有 ${data.failed.count} 笔扣费失败`}
          description={failedHint}
          action={<Button size="small" type="primary" loading={retrying} onClick={() => void retry()}>重新扣费</Button>}
        />
      )}
      <Space size={48} wrap>
        <Statistic title="已扣费笔数" value={data.succeeded.count} />
        <Statistic title="已扣除额度" value={data.succeeded.quota} />
        <Statistic
          title="已计费 tokens（入 / 出）"
          value={`${formatNumber(data.succeeded.prompt_tokens)} / ${formatNumber(data.succeeded.completion_tokens)}`}
        />
        <Statistic title="待扣 / 失败笔数" value={`${data.pending.count} / ${data.failed.count}`} />
        {(data.unbilled_prompt_tokens > 0 || data.unbilled_completion_tokens > 0) && (
          <Statistic
            title="尚未结算的用量（入 / 出）"
            value={`${formatNumber(data.unbilled_prompt_tokens)} / ${formatNumber(data.unbilled_completion_tokens)}`}
          />
        )}
      </Space>
      <Space>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
        {unsettled > 0 && data.failed.count === 0 && (
          <Button loading={retrying} onClick={() => void retry()}>立即重发待扣费记录</Button>
        )}
      </Space>
      <Table
        rowKey="id" size="small" columns={billingColumns()} dataSource={data.records}
        pagination={false} scroll={{ x: 1000 }}
        footer={data.records.length >= 50 ? () => '只显示最近 50 笔' : undefined}
      />
    </Space>
  )
}
