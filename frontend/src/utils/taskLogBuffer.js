// 执行日志的本地缓冲：把服务端的「实时窗口」帧拼成一份越来越长的完整日志。
//
// 为什么需要它：服务端实时通道只保留尾部窗口（live.PREVIEW_LIMIT），并在模块切换、
// 并行组交错等场合整段重发窗口内容。前端若把每个重发帧当整份日志用，跑到第二个
// 模块时第一个模块的日志就当场消失了。所以帧都带绝对偏移（start/offset，口径是
// 完整输出里的字符位置），本地按偏移把重发帧「盖回」自己那份更长的文本上，窗口
// 之外已收到的内容原样保留。
//
// 缓冲状态：
//   text      展示文本（可能含缺口提示，纯展示用）
//   tailIndex text 里「尾段」的起始下标（尾段 = 与服务端偏移对齐的那部分）
//   tailStart 尾段首字符对应的服务端绝对偏移
//   offset    已知的服务端偏移末端（下次增量拉取的起点）
// 于是「服务端偏移 o」在本地的下标 = tailIndex + (o - tailStart)，仅对尾段成立——
// 缺口提示文字插在 tailIndex 之前，不影响这套换算。

// 窗口外内容拿不回来时的占位（服务端只给偏移，缺口提示由这里渲染——提示文字
// 一旦混进正文就会算错绝对偏移）
const GAP_NOTE = '\n\n…（前段日志超出实时窗口未能取回，执行结束后本页显示完整日志）\n\n'

export function createLogBuffer() {
  return { text: '', tailIndex: 0, tailStart: 0, offset: 0 }
}

/** 快照/重发帧：output 覆盖绝对区间 [start, offset)，head 为与其不相连的前段。 */
export function applyFull(buf, { head = '', output = '', start = 0, offset = 0 }) {
  const end = offset || start + output.length
  if (head) {
    // 快照帧带前段（落盘文件里的已完成模块）：前段 + 缺口提示 + 尾段
    buf.text = head + (start > head.length ? GAP_NOTE : '') + output
    buf.tailIndex = buf.text.length - output.length
    buf.tailStart = start
    buf.offset = end
    return buf
  }
  if (start >= buf.tailStart) {
    const at = buf.tailIndex + (start - buf.tailStart)
    if (at <= buf.text.length) {
      // 常态：只替换窗口覆盖的尾段，窗口之前的历史保持不动
      buf.text = buf.text.slice(0, at) + output
      buf.offset = end
      return buf
    }
    if (buf.text) {
      // 服务端偏移跑在本地已知内容之前（并行组各块裁头后 start 偏大、模块收尾时
      // 完整输出与流式片段长度不一致）：接在已有内容之后并标出缺口，历史不丢。
      // 下一帧的 start 就落在这段新尾段里，会走上面的对齐分支，不会反复追加
      buf.text += GAP_NOTE + output
      buf.tailIndex = buf.text.length - output.length
      buf.tailStart = start
      buf.offset = end
      return buf
    }
  }
  buf.text = (start > 0 ? GAP_NOTE.trimStart() : '') + output
  buf.tailIndex = buf.text.length - output.length
  buf.tailStart = start
  buf.offset = end
  return buf
}

/** 增量帧：紧接已知末端追加。 */
export function applyDelta(buf, { output = '', offset = 0 }) {
  if (!output) return buf
  buf.text += output
  buf.offset = offset || buf.offset + output.length
  return buf
}

/** 清空（切换执行记录 / 重新开始跟随时）。 */
export function resetBuffer(buf) {
  buf.text = ''
  buf.tailIndex = 0
  buf.tailStart = 0
  buf.offset = 0
  return buf
}
