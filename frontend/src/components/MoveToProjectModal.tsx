import { useEffect, useState } from 'react'
import { App, Modal, Typography } from 'antd'
import { api } from '../api'
import ProjectSelect from './ProjectSelect'

type Props = {
  /** 要挪的任务；为空时弹窗关闭 */
  jobIds: string[]
  /** 单个任务时预选它当前所在的项目 */
  current?: string | null
  onClose: () => void
  onMoved: () => void
}

/** 把任务挪进某个项目。任务总要属于一个项目，所以不能清空。 */
export default function MoveToProjectModal({ jobIds, current, onClose, onMoved }: Props) {
  const { message } = App.useApp()
  const [target, setTarget] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const open = jobIds.length > 0

  useEffect(() => { if (open) setTarget(current ?? null) }, [open, current])

  const save = async () => {
    setSaving(true)
    try {
      // 项目只对本人可见，批量接口也只会挪动自己的任务
      const { updated } = await api.moveJobsToProject(jobIds, target)
      if (updated < jobIds.length) {
        message.warning(`只挪动了你自己的 ${updated} 个任务，其余是别人的任务`)
      }
      message.success('已归入项目')
      onMoved()
      onClose()
    } catch { /* 拦截器已提示 */ } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={jobIds.length > 1 ? `把 ${jobIds.length} 个任务归入项目` : '归入项目'}
      open={open} onCancel={onClose} onOk={() => void save()} confirmLoading={saving}
      okText="确定" cancelText="取消" destroyOnClose
    >
      <Typography.Paragraph type="secondary">
        项目只用来归类，不影响任务的运行。
      </Typography.Paragraph>
      <ProjectSelect style={{ width: '100%' }} value={target} onChange={setTarget}
        allowCreate allowClear={false} pickDefault />
    </Modal>
  )
}
