import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Col, Descriptions, Dropdown, Empty, Progress, Result, Row, Space,
  Statistic, Table, Tabs, Tag, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ArrowLeftOutlined, DeleteOutlined, DownloadOutlined, PauseCircleOutlined,
  PlayCircleOutlined, ReloadOutlined, StopOutlined, SwapOutlined,
} from '@ant-design/icons'
import { api, downloadUrl } from '../api'
import type { Job, JobErrorRow, ResultRow } from '../api'
import ChangeModelModal from '../components/ChangeModelModal'
import JobStatusTag from '../components/JobStatusTag'
import { usePolling } from '../hooks/usePolling'
import { ACTIVE_STATUSES, formatBytes, formatDateTime, formatDuration, formatNumber } from '../utils'

export default function JobDetailPage() {
  const { jobId = '' } = useParams()
  const navigate = useNavigate()
  const { modal, message } = App.useApp()

  const [job, setJob] = useState<Job | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [results, setResults] = useState<ResultRow[]>([])
  const [resultTotal, setResultTotal] = useState(0)
  const [resultPage, setResultPage] = useState(1)
  const [errors, setErrors] = useState<JobErrorRow[]>([])
  const [tab, setTab] = useState('results')
  const [changingModel, setChangingModel] = useState(false)

  const loadJob = useCallback(async () => {
    try {
      setJob(await api.job(jobId))
    } catch {
      setNotFound(true)
    }
  }, [jobId])

  const loadResults = useCallback(async (page: number) => {
    const res = await api.jobResults(jobId, (page - 1) * 20, 20)
    setResults(res.rows)
    setResultTotal(res.total)
  }, [jobId])

  useEffect(() => { void loadJob() }, [loadJob])
  useEffect(() => { void loadResults(resultPage).catch(() => undefined) }, [loadResults, resultPage])
  useEffect(() => {
    if (tab === 'errors') api.jobErrors(jobId, 200).then(setErrors).catch(() => undefined)
  }, [tab, jobId, job?.failed_items])

  const isActive = job ? ACTIVE_STATUSES.includes(job.status) : false
  usePolling(() => {
    void loadJob()
    void loadResults(resultPage).catch(() => undefined)
  }, isActive ? 3000 : null)

  if (notFound) {
    return <Result status="404" title="任务不存在" subTitle="它可能已被删除，或你没有访问权限"
      extra={<Button type="primary" onClick={() => navigate('/jobs')}>返回任务列表</Button>} />
  }
  if (!job) return <Card loading />

  const act = async (fn: () => Promise<unknown>, okText: string) => {
    try {
      await fn()
      message.success(okText)
      await loadJob()
    } catch { /* 拦截器已提示 */ }
  }

  const finished = ['succeeded', 'completed', 'canceled', 'failed'].includes(job.status)
  const purged = !!job.files_purged_at
  const hasResults = job.completed_items > 0 || job.failed_items > 0
  // 只有跑完的任务才能下载：结果文件在跑动时一直被追加写入，
  // 中途导出会拿到不完整甚至截断的内容
  const canDownload =
    ['succeeded', 'completed', 'canceled'].includes(job.status) && !purged && hasResults

  const resultColumns: ColumnsType<ResultRow> = [
    { title: 'custom_id', dataIndex: 'custom_id', width: 160, ellipsis: true,
      render: (v: string) => <span className="mono">{v}</span> },
    { title: '模型输出', dataIndex: 'output',
      render: (v: string | null) => <div className="output-cell">{v || <Typography.Text type="secondary">（空）</Typography.Text>}</div> },
    { title: 'tokens', width: 120,
      render: (_, row) => row.usage
        ? `${row.usage.prompt_tokens ?? 0} + ${row.usage.completion_tokens ?? 0}`
        : '—' },
  ]

  const errorColumns: ColumnsType<JobErrorRow> = [
    { title: '行号', dataIndex: 'item_index', width: 80, render: (v: number) => v + 1 },
    { title: 'custom_id', dataIndex: 'custom_id', width: 160, ellipsis: true },
    { title: 'HTTP', dataIndex: 'status_code', width: 80, render: (v: number | null) => v ?? '—' },
    { title: '尝试次数', dataIndex: 'attempts', width: 90 },
    { title: '错误信息', dataIndex: 'message',
      render: (v: string | null) => <div className="output-cell mono">{v}</div> },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Row justify="space-between" align="middle" gutter={[16, 16]}>
          <Col>
            <Space align="center" wrap>
              <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/jobs')} />
              <Typography.Title level={4} style={{ margin: 0 }}>{job.name}</Typography.Title>
              <JobStatusTag status={job.status} />
              {job.queue_position ? <Tag>队列第 {job.queue_position} 位</Tag> : null}
            </Space>
          </Col>
          <Col>
            <Space wrap>
              {canDownload && (
                <Dropdown
                  menu={{
                    items: [
                      { key: 'raw', label: '完整 JSONL（含原始响应）' },
                      { key: 'raw_err', label: '完整 JSONL + 失败条目' },
                      { key: 'simple', label: '精简 JSONL（custom_id + 输出）' },
                      { key: 'csv', label: 'CSV（Excel 可直接打开）' },
                      { key: 'input', label: '原始输入文件' },
                    ],
                    onClick: ({ key }) => {
                      const map: Record<string, string> = {
                        raw: `/jobs/${job.id}/download?fmt=raw`,
                        raw_err: `/jobs/${job.id}/download?fmt=raw&include_errors=true`,
                        simple: `/jobs/${job.id}/download?fmt=simple`,
                        csv: `/jobs/${job.id}/download?fmt=csv&include_errors=true`,
                        input: `/jobs/${job.id}/input`,
                      }
                      window.open(downloadUrl(map[key]), '_blank')
                    },
                  }}
                >
                  <Button type="primary" icon={<DownloadOutlined />}>下载结果</Button>
                </Dropdown>
              )}
              {(job.status === 'running' || job.status === 'queued') && (
                <Button icon={<PauseCircleOutlined />}
                  onClick={() => void act(() => api.pauseJob(job.id), '已请求暂停')}>暂停</Button>
              )}
              {['paused', 'failed', 'canceled'].includes(job.status) && !purged && (
                <Button icon={<PlayCircleOutlined />}
                  onClick={() => void act(() => api.resumeJob(job.id), '已重新入队')}>恢复</Button>
              )}
              {['paused', 'failed', 'canceled'].includes(job.status) && !purged && (
                <Button icon={<SwapOutlined />} onClick={() => setChangingModel(true)}>更换模型</Button>
              )}
              {finished && job.failed_items > 0 && !purged && (
                <Button icon={<ReloadOutlined />}
                  onClick={() => void act(() => api.retryFailed(job.id), '已重新入队失败条目')}>
                  重试 {formatNumber(job.failed_items)} 条失败
                </Button>
              )}
              {!finished && (
                <Button danger icon={<StopOutlined />}
                  onClick={() => void act(() => api.cancelJob(job.id), '已请求取消')}>取消</Button>
              )}
              {finished && (
                <Button danger icon={<DeleteOutlined />} onClick={() => modal.confirm({
                  title: '删除该任务？',
                  content: '任务记录、输入文件与结果都会被永久删除。',
                  okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
                  onOk: async () => { await api.deleteJob(job.id); navigate('/jobs') },
                })}>删除</Button>
              )}
            </Space>
          </Col>
        </Row>

        <Progress
          style={{ marginTop: 20 }}
          percent={job.progress}
          status={job.status === 'running' ? 'active' : job.failed_items ? 'exception' : undefined}
        />

        <Row gutter={16} style={{ marginTop: 16 }}>
          <Col xs={12} sm={6}><Statistic title="总条目" value={job.total_items} /></Col>
          <Col xs={12} sm={6}>
            <Statistic title="成功" value={job.completed_items} valueStyle={{ color: '#52c41a' }} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic title="失败" value={job.failed_items}
              valueStyle={{ color: job.failed_items ? '#ff4d4f' : undefined }} />
          </Col>
          <Col xs={12} sm={6}>
            <Statistic title="消耗 tokens"
              value={job.prompt_tokens + job.completion_tokens}
              suffix={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
                （入 {formatNumber(job.prompt_tokens)} / 出 {formatNumber(job.completion_tokens)}）
              </Typography.Text>} />
          </Col>
        </Row>

        {!canDownload && hasResults && !purged && !finished && (
          <Alert
            style={{ marginTop: 16 }} type="info" showIcon
            message="任务结束或取消后才能下载结果"
          />
        )}
        {purged && (
          <Alert
            style={{ marginTop: 16 }} type="warning" showIcon
            message="文件已超过保留期被自动清理"
            description={`清理于 ${formatDateTime(job.files_purged_at)}。文件是不可恢复的。`}
          />
        )}
        {job.error && (
          <Alert style={{ marginTop: 16 }} type="error" showIcon message="任务错误" description={job.error} />
        )}
      </Card>

      <Card>
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'results',
              label: `结果预览 (${formatNumber(resultTotal)})`,
              children: results.length === 0
                ? <Empty description="暂无结果" />
                : (
                  <Table
                    rowKey="index" size="small" columns={resultColumns} dataSource={results}
                    pagination={{
                      current: resultPage, pageSize: 20, total: resultTotal, showSizeChanger: false,
                      onChange: setResultPage,
                      showTotal: (t) => `共 ${formatNumber(t)} 条成功结果`,
                    }}
                  />
                ),
            },
            {
              key: 'errors',
              label: `失败明细 (${formatNumber(job.failed_items)})`,
              children: errors.length === 0
                ? <Empty description="没有失败条目" />
                : <Table rowKey="item_index" size="small" columns={errorColumns} dataSource={errors}
                    pagination={{ pageSize: 20 }} />,
            },
            {
              key: 'info',
              label: '任务信息',
              children: (
                <Descriptions bordered column={{ xs: 1, sm: 2 }} size="small">
                  <Descriptions.Item label="任务 ID"><span className="mono">{job.id}</span></Descriptions.Item>
                  <Descriptions.Item label="提交者">{job.username ?? '—'}</Descriptions.Item>
                  <Descriptions.Item label="模型">
                    <Space size={4}>
                      <Tag color={job.model_source === 'shared' ? 'blue' : 'default'}>
                        {job.model_source === 'shared' ? '公用' : '个人 token'}
                      </Tag>
                      <span className={job.model_source === 'personal' ? 'mono' : undefined}>
                        {job.model_display_name ?? '—'}
                      </span>
                    </Space>
                  </Descriptions.Item>
                  <Descriptions.Item label="并发数">{job.concurrency || '按模型配置'}</Descriptions.Item>
                  <Descriptions.Item label="优先级">{job.priority}</Descriptions.Item>
                  <Descriptions.Item label="执行 worker">
                    <span className="mono">{job.worker_id ?? '—'}</span>
                  </Descriptions.Item>
                  <Descriptions.Item label="输入文件">
                    {job.input_filename}（{formatBytes(job.input_size)}）
                  </Descriptions.Item>
                  <Descriptions.Item label="耗时">
                    {formatDuration(job.started_at, job.finished_at)}
                  </Descriptions.Item>
                  <Descriptions.Item label="创建时间">{formatDateTime(job.created_at)}</Descriptions.Item>
                  <Descriptions.Item label="开始时间">{formatDateTime(job.started_at)}</Descriptions.Item>
                  <Descriptions.Item label="结束时间">{formatDateTime(job.finished_at)}</Descriptions.Item>
                  <Descriptions.Item label="推理参数" span={2}>
                    <pre className="mono pre-wrap" style={{ margin: 0 }}>
                      {JSON.stringify(job.params, null, 2)}
                    </pre>
                  </Descriptions.Item>
                </Descriptions>
              ),
            },
          ]}
        />
      </Card>

      <ChangeModelModal
        job={job}
        open={changingModel}
        onClose={() => setChangingModel(false)}
        onChanged={() => void loadJob()}
      />
    </Space>
  )
}
