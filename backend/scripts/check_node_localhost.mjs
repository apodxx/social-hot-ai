/**
 * Does Node (the stack DSH's MCP client uses) route localhost through the registry proxy?
 *
 * This matters: Python's httpx picks up the Windows registry proxy via
 * urllib.getproxies() and turns a local MCP call into a 502 with an empty body and no
 * server-side log. If Node behaved the same way, the DSH MCP integration would fail for
 * reasons that look like a broken server. The two stacks are not the same, so measure
 * instead of assuming.
 *
 * Usage: node check_node_localhost.mjs
 */

const url = 'http://127.0.0.1:8000/mcp/'

const body = {
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2025-06-18',
    capabilities: {},
    clientInfo: { name: 'node-proxy-check', version: '1' },
  },
}

console.log('proxy env vars seen by Node:')
for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy']) {
  console.log(`  ${key}=${process.env[key] ?? '(unset)'}`)
}

try {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  console.log(`\nPOST ${url} -> HTTP ${response.status}, ${text.length} bytes`)
  console.log('server identified itself:', text.includes('socialhot'))
  if (response.status !== 200) {
    console.log('body:', text.slice(0, 300))
    process.exitCode = 1
  }
} catch (error) {
  console.log(`\nPOST ${url} -> ${error.name}: ${error.message}`)
  if (error.cause) console.log('cause:', error.cause?.message ?? error.cause)
  process.exitCode = 1
}
