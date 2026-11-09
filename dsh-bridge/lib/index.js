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

/** Cordis 插件名（与 bundle patch 里的 id 一致即可） */
export const name = 'pet-bridge'

/** 依赖的服务：会话仓库 + 会话控制器（后者才有 prompt/follow） */
export const inject = ['sessions', 'sessionController']

const STATUS_FILE = path.join(os.homedir(), '.dsh', 'pet-bridge.json')
const DEFAULT_PORT = 47110
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
  const cfg = { port: DEFAULT_PORT, host: '127.0.0.1', statusFile: STATUS_FILE, ...config }
  const log = (...args) => console.log('[pet-bridge]', ...args)

  /** 最近一个「有过动静」的会话，桌宠不指定时就用它 */
  let lastSessionId = null
  /** 正在推的 SSE 连接 */
  const streams = new Set()
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

  server = http.createServer((req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1')
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
        port: actualPort,
        lastSessionId,
        streams: streams.size,
        pid: process.pid,
      })
    }
    if (url.pathname === '/sessions' && req.method === 'GET') return handleSessions(res)
    if (url.pathname === '/stream' && req.method === 'GET') return handleStream(req, res, url)

    if (req.method === 'POST') {
      return readBody(req).then((body) => {
        if (url.pathname === '/session/new') return handleNewSession(res, body)
        if (url.pathname === '/prompt') return handlePrompt(res, body)
        if (url.pathname === '/cancel') return handleCancel(res, body)
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
      try { server?.close() } catch { /* ignore */ }
      try { fs.rmSync(cfg.statusFile, { force: true }) } catch { /* ignore */ }
      for (const dispose of disposers) {
        try { dispose() } catch { /* ignore */ }
      }
      log('已停止')
    }
  })
}
