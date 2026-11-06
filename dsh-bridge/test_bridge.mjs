/**
 * dsh-pet-bridge 的离线自测：不启动 DSH，用 mock 的 ctx 验证插件逻辑。
 *
 * 覆盖：健康检查、会话列表、无 sessionId 时自动建会话、投递 prompt、
 *       SSE 流式转发、打断、以及「服务不重复监听」这类基础行为。
 *
 * 跑法：node test_bridge.mjs
 */
import assert from 'node:assert'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const PORT = 47199
const BASE = `http://127.0.0.1:${PORT}`
const STATUS_FILE = path.join(os.homedir(), '.dsh', 'pet-bridge.json')

let ok = 0
let fail = 0
function check(name, cond, extra = '') {
  if (cond) { ok += 1; console.log(`[OK ] ${name}${extra ? ' | ' + extra : ''}`) }
  else { fail += 1; console.log(`[FAIL] ${name}${extra ? ' | ' + extra : ''}`) }
}

// ---------- mock 上下文 ----------
const calls = { create: [], prompt: [], cancel: [], follow: [] }
const listeners = []
const state = { listItems: null }   // null = 用默认的假数据

const mockCtx = {
  on(event, handler) {
    listeners.push({ event, handler })
    return () => { }
  },
  effect(fn) {
    // cordis 的 effect 立刻执行并收集 dispose
    mockCtx._dispose = fn()
  },
  sessionController: {
    async list() {
      if (state.listItems) return { items: state.listItems }
      return {
        items: [
          { sessionId: 'sess-1', projections: { title: '之前的对话' }, updatedAt: 1762300000000, cwd: 'C:/tmp', blank: false },
          { sessionId: 'sess-2', projections: { title: '另一个会话' }, updatedAt: 1762200000000, cwd: 'C:/tmp', blank: false },
        ],
      }
    },
    async create(req) {
      calls.create.push(req)
      return { sessionId: 'sess-new' }
    },
    async prompt(req) {
      calls.prompt.push(req)
      return { accepted: true }
    },
    cancel(req) {
      calls.cancel.push(req)
      return { accepted: true }
    },
    async *follow(req, signal) {
      calls.follow.push(req)
      yield { type: 'assistant/chunk', delta: '你' }
      yield { type: 'assistant/chunk', delta: '好' }
      yield { type: 'turn/end' }
    },
  },
}

// ---------- 启动插件 ----------
const mod = await import('./lib/index.js')
check('插件导出了 name / inject / apply',
  mod.name === 'pet-bridge' && Array.isArray(mod.inject) && typeof mod.apply === 'function',
  `name=${mod.name} inject=${JSON.stringify(mod.inject)}`)

mod.apply(mockCtx, { port: PORT })
await new Promise((r) => setTimeout(r, 400))

async function getJson(p, init) {
  const res = await fetch(BASE + p, init)
  return { status: res.status, body: await res.json() }
}

// ---------- 1. health ----------
const health = await getJson('/health')
check('/health 可用', health.status === 200 && health.body.ok === true, JSON.stringify(health.body))
check('状态文件写出来了（桌宠靠它找端口）', fs.existsSync(STATUS_FILE))
if (fs.existsSync(STATUS_FILE)) {
  const st = JSON.parse(fs.readFileSync(STATUS_FILE, 'utf8'))
  check('状态文件里的端口正确', st.port === PORT, JSON.stringify(st))
}

// ---------- 2. sessions ----------
const sessions = await getJson('/sessions')
check('/sessions 返回列表', sessions.body.ok && sessions.body.items.length === 2,
  JSON.stringify(sessions.body.items?.map((i) => i.id)))
check('/sessions 带上了标题/时间/是否在跑',
  sessions.body.items?.[0]?.title === '之前的对话'
  && typeof sessions.body.items?.[0]?.updatedAt === 'number'
  && sessions.body.items?.[0]?.running === false,
  JSON.stringify(sessions.body.items?.[0]))

// ---------- 3. prompt（无 sessionId → 自动建会话）----------
const p1 = await getJson('/prompt', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text: '帮我看看今天有什么安排' }),
})
check('没指定会话时接着最近那个用（不新建）', calls.create.length === 0 && p1.body.sessionId === 'sess-1',
  JSON.stringify(p1.body))
check('prompt 内容正确传入 DSH',
  calls.prompt[0]?.content?.[0]?.text === '帮我看看今天有什么安排',
  JSON.stringify(calls.prompt[0]?.content))
check('prompt 带了 requestId 和 mode',
  Boolean(calls.prompt[0]?.requestId) && calls.prompt[0]?.mode === 'queue',
  JSON.stringify({ id: calls.prompt[0]?.requestId, mode: calls.prompt[0]?.mode }))

// ---------- 3.5 一个会话都没有时才新建 ----------
state.listItems = []
const pBlank = await getJson('/prompt', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text: '第一次说话' }),
})
state.listItems = null
check('没有任何会话时会自动建一个',
  calls.create.length === 1 && pBlank.body.sessionId === 'sess-new', JSON.stringify(pBlank.body))

// ---------- 4. 指定 sessionId 时不再新建 ----------
const p2 = await getJson('/prompt', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text: '第二句', sessionId: 'sess-1' }),
})
check('指定 sessionId 时沿用会话、不新建',
  calls.create.length === 1 && p2.body.sessionId === 'sess-1', JSON.stringify(p2.body))

// ---------- 5. 空文本被拒 ----------
const p3 = await getJson('/prompt', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text: '   ' }),
})
check('空文本返回 400', p3.status === 400, JSON.stringify(p3.body))

// ---------- 6. SSE 流 ----------
const streamRes = await fetch(BASE + '/stream?sessionId=sess-1')
check('/stream 返回 event-stream',
  streamRes.headers.get('content-type')?.includes('text/event-stream'),
  streamRes.headers.get('content-type'))

const reader = streamRes.body.getReader()
const decoder = new TextDecoder()
let buffer = ''
const deadline = Date.now() + 4000
while (Date.now() < deadline && !buffer.includes('turn/end')) {
  const { value, done } = await reader.read()
  if (done) break
  buffer += decoder.decode(value, { stream: true })
}
await reader.cancel()
check('流里收到 assistant/chunk', buffer.includes('assistant/chunk'), buffer.slice(0, 120))
check('流里收到 turn/end', buffer.includes('turn/end'))
check('follow 请求带上了 address 和 assistantStream',
  calls.follow[0]?.address?.kind === 'session'
  && calls.follow[0]?.address?.sessionId === 'sess-1'
  && calls.follow[0]?.assistantStream === true,
  JSON.stringify(calls.follow[0]))

// ---------- 7. cancel ----------
const c1 = await getJson('/cancel', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ sessionId: 'sess-1' }),
})
check('/cancel 调到了 DSH', c1.body.ok && calls.cancel[0]?.sessionId === 'sess-1',
  JSON.stringify(calls.cancel))

// ---------- 8. 未知端点 ----------
const nf = await getJson('/nope')
check('未知端点 404', nf.status === 404, nf.status)

console.log(`\nRESULT: ${fail === 0 ? 'ALL PASS' : `${fail} FAILED`} (${ok} passed)`)
process.exit(fail === 0 ? 0 : 1)
