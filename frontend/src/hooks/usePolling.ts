import { useEffect, useRef } from 'react'

/** 定时执行回调；interval 为 null 时暂停（任务跑完就不用再轮询了）。 */
export function usePolling(callback: () => void, interval: number | null) {
  const saved = useRef(callback)
  useEffect(() => {
    saved.current = callback
  }, [callback])

  useEffect(() => {
    if (interval === null) return
    const id = setInterval(() => saved.current(), interval)
    return () => clearInterval(id)
  }, [interval])
}
