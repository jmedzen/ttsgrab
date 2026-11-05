import { chromium } from 'playwright'
const browser = await chromium.launch({ channel: 'chrome' })
const page = await browser.newPage({ viewport: { width: 1440, height: 950 }, deviceScaleFactor: 2 })
const shots = [
  ['/', 'playground'],
  ['/compare', 'compare'],
  ['/voices', 'voices'],
  ['/providers', 'providers'],
  ['/usage', 'usage'],
]
for (const [path, name] of shots) {
  await page.goto('http://127.0.0.1:8011' + path)
  await page.waitForTimeout(1800)
  await page.screenshot({ path: `/tmp/shot-${name}.png` })
}
await browser.close()
console.log('done')
