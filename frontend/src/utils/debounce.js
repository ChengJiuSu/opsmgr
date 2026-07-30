/**
 * 防抖：连续调用只在停止调用 wait 毫秒后执行最后一次。
 * 搜索框边输边查用（默认 300ms，兼顾手感与请求量）。
 * 返回的函数带 cancel()，组件卸载时可取消挂起调用。
 */
export function debounce(fn, wait = 300) {
  let timer = null
  const wrapped = (...args) => {
    clearTimeout(timer)
    timer = setTimeout(() => fn(...args), wait)
  }
  wrapped.cancel = () => clearTimeout(timer)
  return wrapped
}
