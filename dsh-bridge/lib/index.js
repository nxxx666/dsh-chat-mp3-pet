/**
 * dsh-pet-bridge：DSH 与桌面宠物之间的本地桥。
 *
 * 为什么需要它：DSH 桌面版是 Electron（file:// + IPC），没有可供外部程序连接的
 * HTTP 端口。要让桌宠和 DSH 共用同一个会话，只能把桥做进 DSH 进程里——也就是
 * 一个插件。插件在进程内，能直接读会话事件、能直接投递用户消息。
 *
 * 对外只暴露四件事（本地环回，默认 127.0.0.1:47110）：
 *   GET  /health            活着没、绑了哪个端口、最近会话
 *   GET  /sessions          最近的会话列表
 *   POST /session/new       新建一个会话（桌宠专用）
 *   POST /prompt            { sessionId?, text } 投递一句话
 *   GET  /stream?sessionId   SSE，把该会话的实时帧推给桌宠（含流式增量）
 *   POST /cancel            { sessionId } 打断当前回合
 *   GET  /approvals         当前正在等桌宠确认的审批
 *   POST /approve           { id, decision } 桌宠点了「允许/拒绝」
 *
 * 审批（权限确认）：
 *   敏感操作在 DSH 里走 approval/request waterfall。桌面版把待确认项画在
 *   会话的输入框上方，所以只有「该会话被开在界面里」时才有人应答；
 *   桌宠投递的会话往往没在界面里打开，请求会一直挂着（回合卡死）。
 *   这里注册一个兜底应答者：先 next() 让界面和其他应答者处理，界面不在场
 *   （unavailable）时转给桌宠，由气泡上的「允许一次 / 拒绝」作答。
 *
 * 设计约束：
 * - 任何异常都不许把 DSH 搞崩：所有入口 try/catch，只打日志
 * - 不依赖任何第三方包，只用 node 内置模块 + ctx 服务
 * - 端口被占就往后找，实际端口写进 ~/.dsh/pet-bridge.json 供桌宠发现
 */
import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

/** Cordis 插件名（与 bundle patch 里的 id 一致即可） */
export const name = 'pet-bridge'

/** 依赖的服务：会话仓库 + 会话控制器（后者才有 prompt/follow） */
export const inject = ['sessions', 'sessionController']

const STATUS_FILE = path.join(os.homedir(), '.dsh', 'pet-bridge.json')
const DEFAULT_PORT = 47110
/** 转给桌宠的审批等多久算超时（超时按「不可用」关掉，工具不会执行） */
const DEFAULT_APPROVAL_TIMEOUT_MS = 90_000
/** 收到审批时桌宠可能正在重连：等它连上流的宽限 */
const PET_GRACE_MS = 4_000
/** 安全序列化：会话帧里可能有 BigInt 或循环引用，序列化失败不能让流断掉 */
function safeJson(value) {
  const seen = new WeakSet()
  return JSON.stringify(value, (_k, v) => {
    if (typeof v === 'bigint') return v.toString()
    if (typeof v === 'object' && v !== null) {
      if (seen.has(v)) return undefined
      seen.add(v)
    }
    return v
  })
}

/** 把「理由」这种字段统一成可读字符串（displayReason 可能是本地化对象） */
function plainText(value) {
  if (typeof value === 'string') return value
  if (value == null) return ''
  if (typeof value === 'object') {
    for (const key of ['text', 'message', 'label', 'zh', 'value', 'reason']) {
      if (typeof value[key] === 'string' && value[key]) return value[key]
    }
    return ''
  }
  return String(value)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > 2 * 1024 * 1024) {
        reject(new Error('body too large'))
        req.destroy()
        return
      }
      chunks.push(c)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim()
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch (err) {
        reject(new Error('invalid json: ' + err.message))
      }
    })
    req.on('error', reject)
  })
}

export function apply(ctx, config = {}) {
  const cfg = {
    port: DEFAULT_PORT,
    host: '127.0.0.1',
    statusFile: STATUS_FILE,
    approvalTimeoutMs: DEFAULT_APPROVAL_TIMEOUT_MS,
    ...config,
  }
  const log = (...args) => console.log('[pet-bridge]', ...args)
  /** 审批链路的关键节点写进文件：Electron 的 stdout 在打包版里看不到 */
  const dbgFile = path.join(os.homedir(), '.dsh', 'pet-bridge.log')
  function dbg(msg) {
    log(msg)
    try {
      fs.appendFileSync(dbgFile, `${new Date().toISOString()} ${msg}\n`, 'utf8')
    } catch { /* 日志失败不影响主流程 */ }
  }

  /** 最近一个「有过动静」的会话，桌宠不指定时就用它 */
  let lastSessionId = null
  /** 正在推的 SSE 连接 */
  const streams = new Set()
  /** 正在等桌宠点「允许/拒绝」的审批：id -> { finish, sessionId, request } */
  const pendingApprovals = new Map()
  /** 桌宠最近一次活动的时刻（连流 / 调接口都算），用来判断它到底在不在 */
  let lastPetAt = 0
  let actualPort = null
  let server = null

  const disposers = []

  // ---- 记住最近活跃的会话 ----
  try {
    disposers.push(ctx.on('session/event', (session, event) => {
      try {
        const sid = session?.id ?? session?.sessionId ?? event?.sessionId
        if (sid) lastSessionId = String(sid)
      } catch { /* 事件形状变了也不影响主流程 */ }
    }))
  } catch (err) {
    log('订阅 session/event 失败（不影响手动指定 sessionId）：', err?.message)
  }

  // ---- 审批（权限确认）兜底应答 ----
  // 语义：界面优先。先跑完剩下的 waterfall（界面把这条会话开在界面上时
  // 会弹窗并作答），只有拿不到答案（unavailable）才转给桌宠。
  function hasStreamFor(sessionId) {
    for (const entry of streams) {
      if (entry.sessionId === sessionId) return true
    }
    return false
  }

  function broadcast(sessionId, payload) {
    const data = `data: ${safeJson(payload)}\n\n`
    for (const entry of streams) {
      if (sessionId && entry.sessionId !== sessionId) continue
      try { entry.res.write(data) } catch { /* 连接已断，忽略 */ }
    }
  }

  async function waitForStream(sessionId, timeoutMs) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      if (hasStreamFor(sessionId)) return true
      await new Promise((r) => setTimeout(r, 100))
    }
    return hasStreamFor(sessionId)
  }

  /** 把一次审批转成桌宠气泡上的「允许一次 / 拒绝」，返回可撤回的句柄 */
  function startPetAsk(request) {
    const sessionId = String(request?.agent?.session?.id ?? '')
    let id = ''
    let finishRef = null
    let settled = false

    const promise = (async () => {
      if (!sessionId) return 'unavailable'
      // 桌宠没在跑（没连过流、也没有近期活动）→ 别干等，直接按「没人应答」关掉
      const petKnown = hasStreamFor(sessionId) || Date.now() - lastPetAt < 180_000
      if (!petKnown) return 'unavailable'
      if (!hasStreamFor(sessionId) && !(await waitForStream(sessionId, PET_GRACE_MS))) {
        return 'unavailable'
      }
      if (settled) return 'cancelled'

      id = randomUUID()
      const toolName = plainText(request.toolName) || '工具'
      // displayReason 是本地化展示文案，可能是 {text:"…"} 这类对象；
      // 拿不到就退回 reason（纯字符串，通常是模型给的原因）
      const reason = plainText(request.displayReason) || plainText(request.reason)
      dbg(`审批转桌宠：${toolName} @ ${sessionId.slice(0, 12)}`)

      return await new Promise((resolve) => {
        let done = false
        const finish = (outcome, notice) => {
          if (done) return
          done = true
          settled = true
          pendingApprovals.delete(id)
          clearTimeout(timer)
          try { request.signal?.removeEventListener?.('abort', onAbort) } catch { /* ignore */ }
          if (notice) broadcast(sessionId, notice)
          resolve(outcome)
        }
        finishRef = finish
        const timer = setTimeout(() => {
          finish('unavailable', { type: 'approval/timeout', id })
        }, cfg.approvalTimeoutMs)
        const onAbort = () => finish('cancelled', { type: 'approval/cancel', id })
        pendingApprovals.set(id, { finish, sessionId, toolName, reason })
        try { request.signal?.addEventListener?.('abort', onAbort, { once: true }) } catch { /* ignore */ }
        if (request.signal?.aborted) {
          onAbort()
          return
        }
        broadcast(sessionId, {
          type: 'approval',
          id,
          sessionId,
          toolName,
          reason,
          callId: request.callId ?? null,
          timeoutMs: cfg.approvalTimeoutMs,
          createdAt: Date.now(),
        })
      })
    })()

    return {
      promise,
      /** 别处已经答了（界面那边）：把桌宠气泡撤回，别让用户看到过期的确认 */
      cancel(why = 'elsewhere') {
        if (finishRef && id) {
          finishRef('cancelled', { type: 'approval/cancel', id, reason: why })
        }
      },
    }
  }

  /**
   * waterfall 监听器。
   *
   * 必须抢在界面监听器之前（prepend）：Cordis 的 waterfall 是顺序执行，
   * 界面那边「创建 pending 等用户点」时不调 next()，排它后面就永远轮不到，
   * 请求会一直挂到回合卡死。
   *
   * 所以这里并行两条路：自己问桌宠 + 继续跑界面那条链，谁先给出结果用谁；
   * 界面先答就把桌宠气泡撤回。
   */
  async function handleApproval(request, next) {
    const tag = String(request?.toolName ?? '?')
    dbg(`approval 进入 ${tag}`)

    const ui = Promise.resolve()
      .then(() => (typeof next === 'function' ? next() : 'unavailable'))
      .then((o) => (typeof o === 'string' ? o : 'unavailable'), (err) => {
        dbg(`approval 界面链抛错 ${err?.message}`)
        return 'unavailable'
      })

    let pet
    try {
      pet = startPetAsk(request)
    } catch (err) {
      dbg(`approval 转桌宠失败 ${err?.message}`)
      return await ui
    }

    const first = await Promise.race([
      ui.then((o) => ({ src: 'ui', o })),
      pet.promise.then((o) => ({ src: 'pet', o })),
    ])
    if (first.o !== 'unavailable') {
      if (first.src === 'ui') pet.cancel('elsewhere')
      dbg(`approval -> ${first.o} (${first.src})`)
      return first.o
    }

    // 先回来的那条是「没人应答」：继续等另一条（不设额外超时，和桌面版行为一致）
    const other = first.src === 'ui' ? await pet.promise : await ui
    dbg(`approval -> ${other} (${first.src === 'ui' ? 'pet' : 'ui'})`)
    return other === 'unavailable' ? 'unavailable' : other
  }

  try {
    // prepend：必须排在界面监听器前面，否则界面一挂 pending 就没人能兜底
    disposers.push(ctx.on(
      'approval/request',
      (request, next) => handleApproval(request, next),
      { prepend: true },
    ))
    dbg('审批兜底应答者已注册')
  } catch (err) {
    log('订阅 approval/request 失败（敏感操作将无法在桌宠里确认）：', err?.message)
  }

  // ---- HTTP ----
  function send(res, code, obj) {
    const body = Buffer.from(safeJson(obj), 'utf8')
    res.writeHead(code, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': body.length,
      'cache-control': 'no-store',
    })
    res.end(body)
  }

  async function handleSessions(res) {
    const items = []
    try {
      const value = await ctx.sessionController.list({}, new AbortController().signal)
      for (const item of value?.items ?? []) {
        // SessionSummary 本身没有标题字段，标题在 projections 里（可能没有）
        const proj = item.projections || {}
        items.push({
          id: String(item.sessionId ?? item.id ?? ''),
          title: proj.title || proj.name || '',
          updatedAt: item.updatedAt ?? null,
          running: Boolean(item.running),
          blank: Boolean(item.blank),
          cwd: item.cwd || '',
          origin: item.origin || '',
        })
      }
      // 最新在前，桌宠选会话时第一个就是「上次聊的」
      items.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0))
    } catch (err) {
      return send(res, 500, { ok: false, error: String(err?.message || err) })
    }
    return send(res, 200, { ok: true, lastSessionId, items })
  }

  /** 没指定会话时挑一个：桥记着的 → 最近一个非空会话 */
  async function pickSession() {
    if (lastSessionId) return lastSessionId
    try {
      const value = await ctx.sessionController.list({}, new AbortController().signal)
      const usable = (value?.items ?? [])
        .filter((i) => !i.blank && !i.origin)
        .sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0))
      if (usable.length) return String(usable[0].sessionId ?? usable[0].id ?? '')
    } catch { /* 拿不到就新建 */ }
    return ''
  }

  async function createSession(body = {}) {
    const value = await ctx.sessionController.create({
      cwd: body.cwd || process.cwd(),
      agentPreset: body.agentPreset,
    })
    const sessionId = String(value?.sessionId ?? value?.id ?? '')
    if (sessionId) lastSessionId = sessionId
    return sessionId
  }

  async function handleNewSession(res, body) {
    try {
      const sessionId = await createSession(body)
      return send(res, 200, { ok: true, sessionId })
    } catch (err) {
      return send(res, 500, { ok: false, error: String(err?.message || err) })
    }
  }

  async function handlePrompt(res, body) {
    const text = String(body.text ?? '').trim()
    if (!text) return send(res, 400, { ok: false, error: 'text 不能为空' })
    let sessionId = body.sessionId ? String(body.sessionId) : await pickSession()
    if (!sessionId) {
      // 桌宠第一次说话时可还给不出 sessionId，这里替它开一个
      try {
        sessionId = await createSession(body)
      } catch (err) {
        return send(res, 500, { ok: false, error: '建会话失败：' + String(err?.message || err) })
      }
    }
    if (!sessionId) return send(res, 500, { ok: false, error: '没有可用的会话' })

    try {
      const requestId = `pet-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      await ctx.sessionController.prompt(
        {
          requestId,
          sessionId,
          mode: body.mode === 'steer' ? 'steer' : 'queue',
          content: [{ type: 'text', text }],
          clientTimeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        },
        new AbortController().signal,
      )
      return send(res, 200, { ok: true, sessionId, requestId })
    } catch (err) {
      return send(res, 500, { ok: false, error: String(err?.message || err) })
    }
  }

  async function handleStream(req, res, url) {
    const sessionId = url.searchParams.get('sessionId') || lastSessionId
    if (!sessionId) return send(res, 409, { ok: false, error: '缺少 sessionId' })

    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    res.write(': pet-bridge stream\n\n')

    const abort = new AbortController()
    const entry = { res, sessionId, abort }
    streams.add(entry)
    req.on('close', () => {
      streams.delete(entry)
      abort.abort()
    })

    // SSE 心跳：每 15s 发一行注释。作用有两个——
    //   1. 中间层/代理不掐空闲连接；2. 被动监听的桌宠（不发话、只订阅）
    //      靠这些行检查 stop 标志，不然 stream 会阻塞在 iter_lines 里收不到打断。
    const keepalive = setInterval(() => {
      lastPetAt = Date.now()
      try { res.write(': ping\n\n') } catch { /* 连接已断，忽略 */ }
    }, 15000)

    try {
      const iterator = ctx.sessionController.follow(
        { address: { kind: 'session', sessionId }, assistantStream: true },
        abort.signal,
      )
      for await (const frame of iterator) {
        if (abort.signal.aborted) break
        res.write(`data: ${safeJson(frame)}\n\n`)
      }
    } catch (err) {
      if (!abort.signal.aborted) {
        try { res.write(`data: ${safeJson({ type: 'bridge/error', error: String(err?.message || err) })}\n\n`) } catch { /* ignore */ }
      }
    } finally {
      clearInterval(keepalive)
      streams.delete(entry)
      try { res.end() } catch { /* ignore */ }
    }
  }

  async function handleCancel(res, body) {
    const sessionId = body.sessionId ? String(body.sessionId) : lastSessionId
    if (!sessionId) return send(res, 409, { ok: false, error: '缺少 sessionId' })
    try {
      ctx.sessionController.cancel({ sessionId })
      return send(res, 200, { ok: true })
    } catch (err) {
      return send(res, 500, { ok: false, error: String(err?.message || err) })
    }
  }

  /** 桌宠点了「允许一次 / 拒绝」 */
  function handleApprove(res, body) {
    const id = String(body.id ?? '')
    if (!id) return send(res, 400, { ok: false, error: '缺少 id' })
    const item = pendingApprovals.get(id)
    if (!item) {
      // 超时、被打断、或用户已经点过：不是错误，告诉桌宠收掉气泡即可
      return send(res, 404, { ok: false, error: '这个确认已经结束' })
    }
    const allow = body.decision === 'allow'
    const outcome = allow ? 'allowed-once' : 'rejected'
    dbg(`桌宠作答：${item.toolName} -> ${outcome}`)
    item.finish(outcome, { type: 'approval/decided', id, outcome })
    return send(res, 200, { ok: true, outcome })
  }

  function handleApprovals(res) {
    const items = []
    for (const [id, item] of pendingApprovals) {
      items.push({ id, sessionId: item.sessionId, toolName: item.toolName, reason: item.reason })
    }
    return send(res, 200, { ok: true, items })
  }

  server = http.createServer((req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1')
    lastPetAt = Date.now()      // 这个端口只有桌宠会连，任何请求都算「它还在」
    // 只有一个客户端且在本机，不做 CORS 也能跑；这里给上以防浏览器调试
    res.setHeader('access-control-allow-origin', '*')
    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'access-control-allow-methods': 'GET,POST,OPTIONS',
        'access-control-allow-headers': 'content-type',
      })
      return res.end()
    }
    if (url.pathname === '/health') {
      return send(res, 200, {
        ok: true,
        name: 'dsh-pet-bridge',
        version: '0.2.0',
        port: actualPort,
        lastSessionId,
        streams: streams.size,
        pendingApprovals: pendingApprovals.size,
        pid: process.pid,
      })
    }
    if (url.pathname === '/sessions' && req.method === 'GET') return handleSessions(res)
    if (url.pathname === '/approvals' && req.method === 'GET') return handleApprovals(res)
    if (url.pathname === '/stream' && req.method === 'GET') return handleStream(req, res, url)

    if (req.method === 'POST') {
      return readBody(req).then((body) => {
        if (url.pathname === '/session/new') return handleNewSession(res, body)
        if (url.pathname === '/prompt') return handlePrompt(res, body)
        if (url.pathname === '/cancel') return handleCancel(res, body)
        if (url.pathname === '/approve') return handleApprove(res, body)
        return send(res, 404, { ok: false, error: 'unknown endpoint' })
      }).catch((err) => send(res, 400, { ok: false, error: String(err?.message || err) }))
    }
    return send(res, 404, { ok: false, error: 'unknown endpoint' })
  })

  function writeStatus() {
    try {
      fs.mkdirSync(path.dirname(cfg.statusFile), { recursive: true })
      fs.writeFileSync(cfg.statusFile, JSON.stringify({
        port: actualPort, host: cfg.host, pid: process.pid,
        startedAt: new Date().toISOString(),
      }, null, 2), 'utf8')
    } catch (err) {
      log('写状态文件失败（桌宠需要手动填端口）：', err?.message)
    }
  }

  /** 端口被占就往后找一个，最多试 10 个 */
  function startListen(port, attempt = 0) {
    actualPort = port
    const onError = (err) => {
      if (err?.code === 'EADDRINUSE' && attempt < 10) {
        server.removeListener('error', onError)
        log(`端口 ${port} 被占用，试 ${port + 1}`)
        startListen(port + 1, attempt + 1)
        return
      }
      log('HTTP 服务出错：', err?.message)
    }
    server.once('error', onError)
    server.listen(port, cfg.host, () => {
      server.removeListener('error', onError)
      server.on('error', (err) => log('HTTP 运行期错误：', err?.message))
      log(`已监听 http://${cfg.host}:${actualPort}`)
      writeStatus()
    })
  }

  try {
    startListen(cfg.port)
  } catch (err) {
    log('监听失败：', err?.message)
  }

  // 插件被卸载 / DSH 退出时收干净
  ctx.effect(() => {
    return () => {
      for (const entry of streams) {
        try { entry.abort.abort() } catch { /* ignore */ }
      }
      streams.clear()
      // 还有挂着没答的审批：一律按「不可用」关掉，别让 DSH 的回合悬空
      for (const item of [...pendingApprovals.values()]) {
        try { item.finish('unavailable') } catch { /* ignore */ }
      }
      pendingApprovals.clear()
      try { server?.close() } catch { /* ignore */ }
      try { fs.rmSync(cfg.statusFile, { force: true }) } catch { /* ignore */ }
      for (const dispose of disposers) {
        try { dispose() } catch { /* ignore */ }
      }
      log('已停止')
    }
  })
}
