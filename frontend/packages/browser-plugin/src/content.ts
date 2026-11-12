/** @format */
import { handle, moveListener } from './content/contentInject'

chrome.runtime.onMessage.addListener((message, _sender, senderResponse) => {
  const handleMessage = async () => {
    try {
      const result = await handle(message)
      return result
    }
    catch (error) {
      return { error: error.message }
    }
  }

  handleMessage().then(senderResponse)
  return true
})

window.addEventListener(
  'mousemove',
  (event: MouseEvent) => {
    if (!event.isTrusted) {
      console.log('[Astron Debug] 检测到合成事件，已过滤（可能是客户代码转发的事件）')
      return
    }
    if (event.view && event.view !== window) {
      console.log('[Astron Debug] 事件来自其他窗口，已过滤')
      return
    }
    const target = event.target as HTMLElement
    if (!target || !document.contains(target)) {
      return
    }
    if (target.ownerDocument !== document) {
      return
    }
    moveListener(event, document, '')
  },
  true,
)
