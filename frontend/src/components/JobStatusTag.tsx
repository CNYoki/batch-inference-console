import { Tag } from 'antd'
import type { JobStatus } from '../api'
import { STATUS_COLOR, STATUS_LABEL } from '../utils'

export default function JobStatusTag({ status }: { status: JobStatus }) {
  return <Tag color={STATUS_COLOR[status]}>{STATUS_LABEL[status]}</Tag>
}
