/**
 * 审批兜底链路的单测：不启动 DSH，用 mock ctx 跑 pet-bridge。
 *
 * 覆盖：
 *   1. DSH 界面在场（waterfall 前面的应答者给出 allowed-once）→ 桥不打扰桌宠
 *   2. 界面不在场（unavailable）→ 桥把审批推给桌宠 → 桌宠点「允许」→ allowed-once
 *   3. 桌宠点「拒绝」→ rejected
 *   4. 桌宠超时不应答 → unavailable，并推 approval/timeout
 *   5. 桌宠没在跑（没人连流）→ 立即 unavailable，不干等
 *
 * 跑法：node test_approval.mjs
 */
import { apply } from './lib/index.js'
import { fileURLToPath } from 'node:url'

const PORT = Number(process.env.PET_TEST_PORT || 47197)
const BASE = `http://127.0.0.1:${PORT}`
const STATUS = fileURLToPath(new URL('./_test_approval_status.json', import.meta.url))
const SID = 'sess-approval-test'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let pass = 0
let fail = 0
const check = (name, ok, extra = '') => {
  console.log(`[${ok ? 'OK' : 'FAIL'}] ${name}${extra ? '  ' + extra : ''}`)
  ok ? pass++ : fail++
}

// ---- mock ctx ----
const approvalListeners = []
const mockCtx = {
  on(name, cb, options = {}) {
    if (name !== 'approval/request') return () => {}
    // 真实 Cordis：prepend 的监听器排在前面（桥必须抢在界面之前）
    if (options.prepend) approvalListeners.unshift(cb)
    else approvalListeners.push(cb)
    return () => {}
  },
  effect(fn) { fn() },
  sessionController: {
    async list() { return { items: [] } },
    async create() { return { sessionId: SID } },
    async prompt() { return { accepted: true } },
    cancel() { return { accepted: true } },
    async *follow() {
      yield { type: 'snapshot', header: { version: 1, id: SID }, cursor: 0, records: [], hasMore: false, projections: {} }
      while (true) await sleep(1000)
    },
  },
}

// 先放一个「DSH 界面应答者」在 waterfall 前面（和真实客户端同构：先问它，再问桥）
let uiAnswer = null   // null = 界面不在场（delegate 给下一个）
approvalListeners.push(async (_req, next) => {
  if (uiAnswer === null) return await next()
  return uiAnswer
})

apply(mockCtx, { port: PORT, statusFile: STATUS, approvalTimeoutMs: 2500 })

/** 依次跑 waterfall：和 Cordis 的语义一致，监听器返回结果即结束 */
async function runWaterfall(request) {
  let i = 0
  const next = () => {
    if (i >= approvalListeners.length) return Promise.resolve('unavailable')
    const cb = approvalListeners[i++]
    return Promise.resolve(cb(request, next))
  }
  return await next()
}

function makeRequest({ toolName = 'pwsh', callId = 'call-1', reason = '写桌面文件', displayReason } = {}) {
  const req = { agent: { session: { id: SID } }, toolName, callId, reason, signal: new AbortController().signal }
  if (displayReason !== undefined) req.displayReason = displayReason
  return req
}

/** 连上该会话的 SSE，收集帧；返回 { frames, close } */
async function openStream(sessionId = SID) {
  const ctrl = new AbortController()
  const frames = []
  const resp = await fetch(`${BASE}/stream?sessionId=${sessionId}`, { signal: ctrl.signal })
  const reader = resp.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  ;(async () => {
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          for (const line of block.split('\n')) {
            if (line.startsWith('data:')) {
              try { frames.push(JSON.parse(line.slice(5).trim())) } catch { /* ignore */ }
            }
          }
        }
      }
    } catch { /* aborted */ }
  })()
  return { frames, close: () => ctrl.abort() }
}

const waitFrame = async (frames, type, timeout = 4000) => {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    const hit = frames.find((f) => f.type === type)
    if (hit) return hit
    await sleep(50)
  }
  return null
}

const post = async (path, body) => {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  return { status: r.status, data: await r.json() }
}

async function main() {
  await sleep(400)

  // 0) 桥自身启动正常
  const health = await (await fetch(`${BASE}/health`)).json()
  check('桥已启动且带版本', health.ok === true && health.version === '0.2.0', `(v${health.version})`)

  // 5) 桌宠没连流（只有过接口调用）→ 等一小段宽限后按不可用关掉，不干等到超时
  const t0 = Date.now()
  const noPet = await runWaterfall(makeRequest())
  const spent = Date.now() - t0
  check('桌宠没连流时不干等（宽限内关掉）', noPet === 'unavailable' && spent < 6000, `(${spent}ms, ${noPet})`)

  // 1) 界面在场：采用界面的答案；桌宠那边即使弹了也要被撤回
  const stream = await openStream()
  await sleep(300)
  uiAnswer = 'allowed-once'
  const uiOutcome = await runWaterfall(makeRequest({ toolName: 'ui-tool' }))
  check('界面在场时采用界面的答案', uiOutcome === 'allowed-once', `(${uiOutcome})`)
  await sleep(400)
  const uiAsk = stream.frames.find((f) => f.type === 'approval')
  const uiCancel = stream.frames.find((f) => f.type === 'approval/cancel')
  check('界面先答后桌宠气泡被撤回',
    !!uiAsk && !!uiCancel && uiCancel.reason === 'elsewhere',
    uiCancel ? `(reason=${uiCancel.reason})` : '(没收到撤回)')

  // 2) 界面不在场 → 推给桌宠 → 允许
  stream.frames.length = 0
  uiAnswer = null
  const pending = runWaterfall(makeRequest({ toolName: 'pwsh', callId: 'call-allow' }))
  const ask = await waitFrame(stream.frames, 'approval')
  check('界面不在场时把审批推给桌宠', !!ask && ask.toolName === 'pwsh', ask ? `(${ask.toolName})` : '')
  check('审批帧带会话与理由', !!ask && ask.sessionId === SID && ask.reason.includes('桌面'))

  const list = await (await fetch(`${BASE}/approvals`)).json()
  check('/approvals 能查到待确认项', list.items.some((i) => i.id === ask.id))

  const allowRes = await post('/approve', { id: ask.id, decision: 'allow' })
  const allowOutcome = await pending
  check('桌宠点允许后得到 allowed-once',
    allowRes.data.outcome === 'allowed-once' && allowOutcome === 'allowed-once',
    `(${allowOutcome})`)
  const decided = await waitFrame(stream.frames, 'approval/decided')
  check('桌宠收到已决定回执', !!decided && decided.outcome === 'allowed-once')

  // 重复作答：应被礼貌拒绝（404），不能改结果
  const again = await post('/approve', { id: ask.id, decision: 'allow' })
  check('重复作答被拒', again.status === 404)

  // 2.5) displayReason 是本地化对象时，不能给桌宠丢一个 [object Object]
  stream.frames.length = 0
  const pendingObj = runWaterfall(makeRequest({
    toolName: 'pwsh', callId: 'call-obj', displayReason: { text: '要在桌面写入文件' },
  }))
  const askObj = await waitFrame(stream.frames, 'approval')
  check('对象形式的展示文案被转成文字',
    !!askObj && askObj.reason === '要在桌面写入文件', askObj ? askObj.reason : '')
  await post('/approve', { id: askObj.id, decision: 'deny' })
  await pendingObj

  // 3) 拒绝
  stream.frames.length = 0
  const pending2 = runWaterfall(makeRequest({ toolName: 'pwsh', callId: 'call-deny' }))
  const ask2 = await waitFrame(stream.frames, 'approval')
  await post('/approve', { id: ask2.id, decision: 'deny' })
  const denyOutcome = await pending2
  check('桌宠点拒绝后得到 rejected', denyOutcome === 'rejected', `(${denyOutcome})`)

  // 4) 没人答 → 超时按不可用关掉
  stream.frames.length = 0
  const pending3 = runWaterfall(makeRequest({ toolName: 'pwsh', callId: 'call-timeout' }))
  await waitFrame(stream.frames, 'approval')
  const timeoutOutcome = await pending3
  check('桌宠不答时超时按不可用关掉', timeoutOutcome === 'unavailable', `(${timeoutOutcome})`)
  const timeoutFrame = await waitFrame(stream.frames, 'approval/timeout', 2000)
  check('超时会通知桌宠收气泡', !!timeoutFrame)

  // 6) 打断：请求中止 → cancelled
  stream.frames.length = 0
  const ctrl = new AbortController()
  const reqAbort = { agent: { session: { id: SID } }, toolName: 'pwsh', callId: 'call-abort', signal: ctrl.signal }
  const pending4 = runWaterfall(reqAbort)
  await waitFrame(stream.frames, 'approval')
  ctrl.abort()
  const abortOutcome = await pending4
  check('审查中途被打断 -> cancelled', abortOutcome === 'cancelled', `(${abortOutcome})`)
  check('打断会通知桌宠收气泡', !!(await waitFrame(stream.frames, 'approval/cancel', 2000)))

  stream.close()
  console.log(`\nRESULT ${pass}/${pass + fail} passed, ${fail} failed`)
  process.exit(fail ? 1 : 0)
}

main().catch((err) => {
  console.error('测试异常：', err)
  process.exit(1)
})
