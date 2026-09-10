import { useEffect, useMemo, useState } from 'react'
import { Alert, App, Checkbox, Divider, Form, Modal, Select, Space, Typography } from 'antd'
import { api } from '../api'
import type { Job, ModelOptions, SystemSettings } from '../api'
import { useAuth } from '../hooks/useAuth'
import {
  buildModelGroups, capsFor, filterModelOption, parseSelection,
} from '../modelSelect'
import { formatNumber } from '../utils'
import JobParamsFields, { formToParams, paramsToForm } from './JobParamsFields'

function currentValue(job: Job): string | undefined {
  if (job.model_source === 'personal') {
    return job.model_display_name ? `personal:${job.model_display_name}` : undefined
  }
  return job.model_config_id ? `shared:${job.model_config_id}` : undefined
}

/** 给暂停/取消/失败的任务换模型、改推理参数，可选改完直接恢复 */
export default function ChangeModelModal(
  { job, open, onClose, onChanged }:
  { job: Job; open: boolean; onClose: () => void; onChanged: () => void },
) {
  const { message } = App.useApp()
  const { user } = useAuth()
  const [form] = Form.useForm()
  const [options, setOptions] = useState<ModelOptions | null>(null)
  const [settings, setSettings] = useState<SystemSettings | null>(null)
  const [value, setValue] = useState<string>()
  const [dirty, setDirty] = useState(false)
  const [resume, setResume] = useState(true)
  const [saving, setSaving] = useState(false)

  // 个人网关模型执行时用的是提交者本人的 token，替别人的任务只能换公用模型（改参数不受限）
  const isOwner = user?.id === job.user_id
  const current = currentValue(job)

  useEffect(() => {
    if (!open) return
    setValue(current)
    setDirty(false)
    setResume(true)
    api.modelOptions().then(setOptions).catch(() => undefined)
    api.settings().then(setSettings).catch(() => undefined)
  }, [open, current])

  const groups = useMemo(() => {
    const list = buildModelGroups(options, isOwner)
    // 当前模型可能已不在可选列表里（被禁用、别人的个人模型），也要能保持不变只改参数
    if (current && !list.some((g) => g.options.some((o) => o.value === current))) {
      const name = job.model_display_name ?? '—'
      list.unshift({
        label: '当前模型',
        options: [{ value: current, name, label: <span>{name}（当前）</span> }],
      })
    }
    return list
  }, [options, isOwner, current, job.model_display_name])

  const caps = useMemo(
    () => capsFor(parseSelection(value), options, settings), [value, options, settings],
  )
  const processed = job.completed_items + job.failed_items

  const onOk = async () => {
    const picked = parseSelection(value)
    if (!picked) return
    let params: Record<string, unknown>
    try {
      await form.validateFields()
      // getFieldsValue(true) 连被隐藏的项一起取；表单里没有的参数（命令行设的 stop 等）保留原值
      params = { ...job.params, ...formToParams(form.getFieldsValue(true)) }
    } catch (err) {
      if (err instanceof SyntaxError) message.error('「附加请求参数」不是合法 JSON')
      return
    }

    setSaving(true)
    let changed = false
    try {
      await api.changeJobModel(job.id, {
        model_source: picked.source,
        model_config_id: picked.source === 'shared' ? picked.key : null,
        personal_model: picked.source === 'personal' ? picked.key : null,
        params,
      })
      changed = true
      if (resume) await api.resumeJob(job.id)
      message.success(resume ? '已保存并重新入队' : '已保存')
    } catch {
      // 拦截器已提示；已保存成功只是恢复失败时，照样收起弹窗刷新任务
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
      title="更换模型 / 修改参数"
      width={720}
      okText={resume ? '保存并恢复' : '保存'}
      cancelText="取消"
      confirmLoading={saving}
      okButtonProps={{ disabled: !value || (value === current && !dirty) }}
      onOk={() => void onOk()}
      onCancel={onClose}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="info" showIcon
          message={processed
            ? `已处理的 ${formatNumber(processed)} 条保留原来的结果，恢复后剩余条目按新的模型和参数处理。`
            : '恢复后所有条目都按新的模型和参数处理。'}
          description={<>
            新模型锁定或不支持的参数以新模型的配置为准。
            {job.failed_items > 0 && (
              <>失败的 {formatNumber(job.failed_items)} 条不会随恢复重跑，任务结束后可以点「重试失败」按新配置重跑。</>
            )}
          </>}
        />
        <div>
          <Typography.Text strong>模型</Typography.Text>
          <Select
            showSearch
            style={{ width: '100%', marginTop: 8 }}
            placeholder={groups.length ? '选择模型' : '暂无可用模型'}
            options={groups}
            value={value}
            onChange={setValue}
            optionFilterProp="name"
            filterOption={filterModelOption}
          />
        </div>
        {!isOwner && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            个人网关模型要用任务提交者本人的 token 调用，替别人的任务只能换成公用模型；参数可以照常修改。
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
      </Space>

      <Divider orientation="left" plain>推理参数</Divider>
      <Form
        form={form}
        layout="vertical"
        initialValues={paramsToForm(job.params)}
        onValuesChange={() => setDirty(true)}
      >
        <JobParamsFields form={form} caps={caps} />
      </Form>

      <Checkbox style={{ marginTop: 16 }} checked={resume} onChange={(e) => setResume(e.target.checked)}>
        保存后立即恢复运行
      </Checkbox>
    </Modal>
  )
}
