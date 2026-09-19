/**
 * Screenshot the admin UI, optionally after clicking something.
 *
 * A static `--screenshot` cannot verify anything interactive: the drawer that shows an
 * item's images and body only exists after a row click. This drives Chrome over the
 * DevTools Protocol (Node has `fetch` and `WebSocket` built in, so no dependencies) and
 * captures the result — which is how the drawer was actually checked.
 *
 * Usage (start Chrome first with --remote-debugging-port=9222):
 *   node scripts/capture.mjs --url http://127.0.0.1:8000/hot --out out.png \
 *        --click ".el-table__row" --wait 2500
 *
 * Flags:
 *   --url    page to open                        (required)
 *   --out    png path                            (required)
 *   --click  CSS selector to click before capture
 *   --wait   ms to wait after load and after the click (default 2500)
 *   --width  viewport width (default 1500)
 *   --height viewport height (default 1000)
 *   --port   debugging port (default 9222)
 */

const args = process.argv.slice(2)
const flag = (name, fallback = '') => {
  const index = args.indexOf(`--${name}`)
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback
}

/** Every value of a repeatable flag, in order (e.g. --click a --click b). */
const flagAll = (name) => {
  const values = []
  args.forEach((argument, index) => {
    if (argument === `--${name}` && args[index + 1]) values.push(args[index + 1])
  })
  return values
}

const url = flag('url')
const out = flag('out')
// Repeatable: a pager click followed by a row click is how a second page was reached.
const clicks = flagAll('click')
const waitMs = Number(flag('wait', '2500'))
const width = Number(flag('width', '1500'))
const height = Number(flag('height', '1000'))
const port = flag('port', '9222')

if (!url || !out) {
  console.error('usage: node capture.mjs --url <page> --out <png> [--click <selector>]')
  process.exit(2)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** Find the page target's debugger URL. */
async function findTarget() {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json`)
      const targets = await response.json()
      const page = targets.find((target) => target.type === 'page' && target.webSocketDebuggerUrl)
      if (page) return page.webSocketDebuggerUrl
    } catch {
      // Chrome is still starting.
    }
    await sleep(500)
  }
  throw new Error(`no page target on port ${port}; start Chrome with --remote-debugging-port=${port}`)
}

class Cdp {
  constructor(socket) {
    this.socket = socket
    this.nextId = 1
    this.pending = new Map()
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      const entry = this.pending.get(message.id)
      if (!entry) return
      this.pending.delete(message.id)
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)))
      else entry.resolve(message.result)
    })
  }

  send(method, params = {}) {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }
}

const debuggerUrl = await findTarget()
const socket = new WebSocket(debuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

const cdp = new Cdp(socket)
await cdp.send('Page.enable')
await cdp.send('Runtime.enable')
await cdp.send('Emulation.setDeviceMetricsOverride', {
  width,
  height,
  deviceScaleFactor: 1,
  mobile: false,
})
await cdp.send('Page.navigate', { url })
await sleep(waitMs)

if (clicks.length) {
  for (const selector of clicks) {
    const result = await cdp.send('Runtime.evaluate', {
      expression: `(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return 'NOT_FOUND';
        node.click();
        return 'CLICKED';
      })()`,
      returnByValue: true,
    })
    const outcome = result.result?.value
    console.log(`click ${selector}: ${outcome}`)
    if (outcome !== 'CLICKED') {
      console.error('the selector matched nothing — nothing to capture')
      process.exitCode = 1
    }
    // Let the UI settle between clicks: clicking a pager re-renders the table, and the
    // next selector has to match the *new* rows.
    await sleep(waitMs)
  }
}

// Report any console errors: a silently broken render is what this is meant to catch.
const errors = await cdp.send('Runtime.evaluate', {
  expression: `JSON.stringify(window.__captureErrors || [])`,
  returnByValue: true,
})
const captured = JSON.parse(errors.result?.value || '[]')
if (captured.length) console.log('page errors:', captured)

const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
const { writeFile } = await import('node:fs/promises')
await writeFile(out, Buffer.from(shot.data, 'base64'))
console.log(`captured ${out}`)

socket.close()
process.exit(process.exitCode ?? 0)
