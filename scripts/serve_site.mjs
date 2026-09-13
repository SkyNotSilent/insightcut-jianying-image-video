// Local QA server: serves only the built site, including seekable video ranges.
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../site-dist')
const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.json': 'application/json' }
http.createServer((req, res) => {
  try {
    let file = path.resolve(root, `.${decodeURIComponent(new URL(req.url, 'http://localhost').pathname)}`)
    if (file !== root && !file.startsWith(root + path.sep)) { res.writeHead(403).end(); return }
    if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html')
    const size = fs.statSync(file).size
    const headers = { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Accept-Ranges': 'bytes' }
    const range = req.headers.range?.match(/^bytes=(\d+)-(\d*)$/)
    let start = 0, end = size - 1
    if (req.headers.range && !range) { res.writeHead(416, { 'Content-Range': `bytes */${size}` }).end(); return }
    if (range) {
      start = Number(range[1]); end = range[2] ? Math.min(Number(range[2]), size - 1) : size - 1
      if (start > end || start >= size) { res.writeHead(416, { 'Content-Range': `bytes */${size}` }).end(); return }
      headers['Content-Range'] = `bytes ${start}-${end}/${size}`
    }
    headers['Content-Length'] = end - start + 1
    res.writeHead(range ? 206 : 200, headers)
    if (req.method === 'HEAD') res.end()
    else fs.createReadStream(file, { start, end }).pipe(res)
  } catch { res.writeHead(404).end('Not found') }
}).listen(2104, '127.0.0.1', () => console.log('Site preview: http://127.0.0.1:2104'))
