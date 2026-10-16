// 任务「等人处理」的判定与文案（执行页面板与独立日志页共用）。
//
// 两种等待：
//   - 暂停确认：status=paused，执行还在跑（占着执行位），可继续 / 中止；
//   - 失败待处理：status=failed 且 pause_type=failed——模块失败后执行已经落终态、
//     释放了执行位与主机锁，但在 resume_deadline 之前仍可重试 / 跳过 / 终止
//     （重试、跳过沿用同一条执行记录续跑）。

/** 失败待处理且未逾期。 */
export function awaitingFailure(task) {
  if (task?.status !== 'failed' || task?.pause_type !== 'failed') return false
  return !task.resume_deadline || new Date(task.resume_deadline) > new Date()
}

/** 截止时间的简短说法：今天给 HH:mm，跨天带上 MM-DD。 */
export function deadlineText(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.toDateString() === new Date().toDateString()
    ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

/** 操作条标题。 */
export function pauseTitle(task) {
  if (awaitingFailure(task)) {
    const until = deadlineText(task.resume_deadline)
    return `模块执行失败，已释放执行资源${until ? `；${until} 前` : '，'}可重试 / 跳过 / 终止（逾期按终止处理）`
  }
  return '任务流已暂停，等待人工确认后继续'
}

export const CONFIRM_DONE_MSG = {
  continue: '已确认，任务流继续执行',
  abort: '已发送中止指令',
  retry: '已提交重试，失败模块将重新排队执行',
  skip: '已提交跳过，任务流将排队继续执行后续模块',
}
