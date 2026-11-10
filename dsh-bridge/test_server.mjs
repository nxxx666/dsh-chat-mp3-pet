/**
 * 给 Python 侧联调用的「假 DSH」：只跑 pet-bridge 插件，ctx 全是 mock。
 *
 * 用来验证 dsh_client.py 能正确读端口、投递 prompt、解析真实形状的流帧
 * （snapshot / event / assistant-stream 三种），不需要真的启动 DSH。
 *
 * 跑法：node test_server.mjs [port]
 */
import { apply } from './lib/index.js'
import { writeFileSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const PORT = Number(process.argv[2] || process.env.PET_TEST_PORT || 47198)
const STATUS = new URL('./_test_pet_bridge.json', import.meta.url).pathname
// 每个 chunk 之间停一下，用来模拟「模型还在思考/输出」的窗口（压测线程生命周期用）
const CHUNK_DELAY = Number(process.env.PET_TEST_CHUNK_DELAY_MS || 0)
// 自由推轮模式：订阅即推一整轮（不等 prompt），用来模拟「用户在 DSH
// 桌面版界面里直接发起对话」——测桌宠的被动监听（思考泡）用
const FREE_TURN = !!process.env.PET_TEST_FREE_TURN
// cancel 调用落盘，Python 侧打断测试据此验证「真的通知了 DSH」
const CANCEL_LOG = fileURLToPath(new URL('./_test_cancel.json', import.meta.url))

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const SESSION_ID = 'sess-pet-test'
// 测试「跟随会话切换」用：往这个文件写 {"sessionId":"xxx"} 就相当于
// 用户在 DSH 界面里切到那条会话（插件据此更新 lastSessionId）
const SWITCH_FILE = fileURLToPath(new URL('./_test_switch.json', import.meta.url))
// 监听这个会话的流永远是静默的（模拟「桌宠死盯旧会话、看不到新对话」）
const QUIET_SESSION = 'sess-quiet'
let cancelled = false
let prompted = false

// 真实的 ctx.on 能收插件注册的回调，这里也实现一个：切会话时触发 session/event
const listeners = new Map()
const mockOn = (name, cb) => {
  if (!listeners.has(name)) listeners.set(name, [])
  listeners.get(name).push(cb)
  return () => {}
}
let seenSwitch = ''
setInterval(() => {
  try {
    const data = JSON.parse(readFileSync(SWITCH_FILE, 'utf8'))
    const sid = String(data.sessionId || '')
    if (sid && sid !== seenSwitch) {
      seenSwitch = sid
      for (const cb of listeners.get('session/event') || []) {
        cb({ id: sid }, { type: 'test/session-switch' })
      }
    }
  } catch { /* 文件不存在就跳过 */ }
}, 300)

function logCancel(sessionId) {
  cancelled = true
  try {
    writeFileSync(CANCEL_LOG, JSON.stringify({ sessionId, at: Date.now() }))
  } catch (e) {
    console.error('[mock] cancel log failed:', e.message)
  }
}

const mockCtx = {
  on: mockOn,
  effect(fn) { fn() },
  sessionController: {
    async list() {
      return {
        items: [
          { id: SESSION_ID, title: '和桌宠的对话', updatedAt: Date.now(), cwd: 'C:/Users/Lenovo/Desktop' },
        ],
      }
    },
    async create() { return { sessionId: SESSION_ID } },
    async prompt(req) {
      console.log('[mock] got prompt:', JSON.stringify(req.content))
      cancelled = false   // 新回合开始，重置打断状态（贴近真实 DSH 行为）
      prompted = true
      return { accepted: true }
    },
    cancel(req) {
      // 真实 DSH 会中止当前回合；这里记下来 + 让流尽快结束
      logCancel(req?.sessionId || SESSION_ID)
      return { accepted: true }
    },
    async *follow(req) {
      const sid = req?.address?.sessionId || SESSION_ID
      // 1. 开场快照
      yield {
        type: 'snapshot',
        header: { version: 1, id: sid, createdAt: Date.now(), isSeeded: false },
        cursor: 0,
        records: [],
        hasMore: false,
        projections: {},
      }
      // 静默会话：只有快照，之后一直挂着（模拟「这条会话没人在聊」）
      if (sid === QUIET_SESSION) {
        while (!cancelled) await sleep(100)
        return
      }
      // 2. 非自由推轮模式下：等真有人发话再推帧（保证聊天类测试时序确定）
      if (!FREE_TURN) {
        while (!prompted && !cancelled) await sleep(50)
        if (cancelled) return
      }
      // 3. 回合开始
      yield {
        type: 'event',
        event: { type: 'turn/start', seq: 1, time: Date.now(), data: {} },
      }
      // 4. 思考增量：真实模型会先吐 reasoning-delta，桌宠不该显示
      for (const piece of ['先想一下', '再想一下']) {
        if (cancelled) return   // 被打断：流就此结束（真实 DSH 是中止回合）
        if (CHUNK_DELAY) await sleep(CHUNK_DELAY)
        yield {
          type: 'assistant-stream',
          frame: { type: 'chunk', attemptId: 'a1', revision: 1, index: 0, time: Date.now(), chunk: { type: 'reasoning-delta', index: 0, text: piece } },
        }
      }
      // 4. 正文增量（两段）
      for (const piece of ['今天', '天气不错哦～']) {
        if (cancelled) return
        if (CHUNK_DELAY) await sleep(CHUNK_DELAY)
        yield {
          type: 'assistant-stream',
          frame: { type: 'chunk', attemptId: 'a1', revision: 1, index: 1, time: Date.now(), chunk: { type: 'text-delta', index: 1, text: piece } },
        }
      }
      // 5. 工具调用
      yield {
        type: 'event',
        event: {
          type: 'tool/call', seq: 2, time: Date.now(),
          data: { name: 'bash', args: { command: 'ls' } },
        },
      }
      // 6. 已提交的完整消息（用于校正）：content 里 reasoning 与 text 混排
      yield {
        type: 'event',
        event: {
          type: 'assistant/message', seq: 3, time: Date.now(),
          data: {
            content: [
              { type: 'reasoning', text: '先想一下再回答' },
              { type: 'text', text: '今天天气不错哦～' },
            ],
          },
        },
      }
      // 7. 回合结束
      yield {
        type: 'event',
        event: { type: 'turn/end', seq: 4, time: Date.now(), data: {} },
      }
    },
  },
}

apply(mockCtx, { port: PORT, statusFile: STATUS })
console.log(`[test-server] listening on ${PORT}, status -> ${STATUS}`)
