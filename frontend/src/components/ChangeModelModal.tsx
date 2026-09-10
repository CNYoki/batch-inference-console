import { useEffect, useMemo, useState } from 'react'
import { Alert, App, Checkbox, Modal, Select, Space, Typography } from 'antd'
import { api } from '../api'
import type { Job, ModelOptions } from '../api'
import { useAuth } from '../hooks/useAuth'
import { buildModelGroups, filterModelOption, parseSelection } from '../modelSelect'
import { formatNumber } from '../utils'

function currentValue(job: Job): string | undefined {
  if (job.model_source === 'personal') {
    return job.model_display_name ? `personal:${job.model_display_name}` : undefined
  }
  return job.model_config_id ? `shared:${job.model_config_id}` : undefined
}

/** 给暂停/取消/失败的任务换模型，可选换完直接恢复 */
export default function ChangeModelModal(
  { job, open, onClose, onChanged }:
  { job: Job; open: boolean; onClose: () => void; onChanged: () => void },
) {
  const { message } = App.useApp()
  const { user } = useAuth()
  const [options, setOptions] = useState<ModelOptions | null>(null)
  const [value, setValue] = useState<string>()
  const [resume, setResume] = useState(true)
  const [saving, setSaving] = useState(false)

  // 个人网关模型执行时用的是提交者本人的 token，替别人的任务只能换公用模型
  const isOwner = user?.id === job.user_id

  useEffect(() => {
    if (!open) return
    setValue(undefined)
    setResume(true)
    api.modelOptions().then(setOptions).catch(() => undefined)
  }, [open])

  const groups = useMemo(() => buildModelGroups(options, isOwner), [options, isOwner])
  const processed = job.completed_items + job.failed_items

  const onOk = async () => {
    const picked = parseSelection(value)
    if (!picked) return
    setSaving(true)
    let changed = false
    try {
      await api.changeJobModel(job.id, {
        model_source: picked.source,
        model_config_id: picked.source === 'shared' ? picked.key : null,
        personal_model: picked.source === 'personal' ? picked.key : null,
      })
      changed = true
      if (resume) await api.resumeJob(job.id)
      message.success(resume ? '已更换模型并重新入队' : '已更换模型')
    } catch {
      // 拦截器已提示；模型已换成功只是恢复失败时，照样收起弹窗刷新任务
    } finally {
      setSaving(false)
      if (changed) {
        onClose()
        onChanged()
      }
    }
  }

  return (
    <Modal
      open={open}
      title="更换模型"
      okText={resume ? '更换并恢复' : '更换'}
      cancelText="取消"
      confirmLoading={saving}
      okButtonProps={{ disabled: !value || value === currentValue(job) }}
      onOk={() => void onOk()}
      onCancel={onClose}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="info" showIcon
          message={processed
            ? `已处理的 ${formatNumber(processed)} 条保留原模型的结果，恢复后剩余条目改用新模型。`
            : '恢复后所有条目都会用新模型处理。'}
          description={<>
            推理参数沿用原任务的设置，新模型锁定或不支持的参数以新模型的配置为准。
            {job.failed_items > 0 && (
              <>失败的 {formatNumber(job.failed_items)} 条不会随恢复重跑，任务结束后可以点「重试失败」用新模型重跑。</>
            )}
          </>}
        />
        <div>
          <Typography.Text type="secondary">当前模型：</Typography.Text>
          <span className={job.model_source === 'personal' ? 'mono' : undefined}>
            {job.model_display_name ?? '—'}
          </span>
        </div>
        <Select
          showSearch
          style={{ width: '100%' }}
          placeholder={groups.length ? '选择新模型' : '暂无可用模型'}
          options={groups}
          value={value}
          onChange={setValue}
          optionFilterProp="name"
          filterOption={filterModelOption}
        />
        {!isOwner && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            个人网关模型要用任务提交者本人的 token 调用，替别人的任务只能换成公用模型。
          </Typography.Text>
        )}
        {isOwner && options?.gateway_enabled && options.personal_error && (
          <Alert type="warning" showIcon message="个人模型列表拉取失败" description={options.personal_error} />
        )}
        {isOwner && options?.gateway_enabled && !options.has_saved_token && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            要换成个人网关模型，请先在「新建任务」页保存你的 {options.gateway_label} token。
          </Typography.Text>
        )}
        <Checkbox checked={resume} onChange={(e) => setResume(e.target.checked)}>
          更换后立即恢复运行
        </Checkbox>
      </Space>
    </Modal>
  )
}
