import { expect, test } from '@playwright/test'

/**
 * Playground e2e: the acceptance criterion is "with only Edge TTS configured, you can
 * synthesise in the UI and hear it". So this test really does click Synthesise and
 * asserts that an audio element with bytes appears.
 */
test.describe('Playground', () => {
  test('loads and shows the voice catalogue', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /Playground|試聽台/ })).toBeVisible()
    await expect(page.getByTestId('text-input')).toBeVisible()
    await expect(page.getByTestId('synthesize')).toBeVisible()
  })

  test('synthesises Traditional Chinese and renders a player', async ({ page }) => {
    await page.goto('/')
    const textarea = page.getByTestId('text-input')
    await textarea.fill('你好，這是自動化測試。')

    // Pick a known-good zh-TW Edge voice explicitly rather than relying on the default,
    // because the edge endpoint intermittently drops specific voices.
    await page.getByTestId('voice-select').click()
    await page.getByTestId('voice-search').fill('zh-TW-HsiaoChen')
    await page.getByRole('button', { name: /HsiaoChen/ }).first().click()

    const responsePromise = page.waitForResponse(
      (r) => r.url().includes('/v1/tts') && r.request().method() === 'POST',
    )
    await page.getByTestId('synthesize').click()
    const response = await responsePromise
    expect(response.status()).toBe(200)
    expect(response.headers()['x-ttsgrab-provider']).toBeTruthy()

    const player = page.getByTestId('play-button')
    await expect(player).toBeVisible({ timeout: 30_000 })

    const audio = page.locator('audio')
    await expect(audio).toHaveAttribute('src', /^blob:/)
    const bytes = await audio.evaluate(async (el: HTMLAudioElement) => {
      const res = await fetch(el.src)
      return (await res.arrayBuffer()).byteLength
    })
    expect(bytes).toBeGreaterThan(1000)
  })

  test('switches UI language without reload', async ({ page }) => {
    await page.goto('/')
    const heading = page.getByRole('heading', { level: 1 })
    // The initial language follows the browser locale, so assert on the *change*
    // rather than assuming which language we start in.
    const before = await heading.textContent()
    await page.getByTestId('lang-toggle').click()
    await expect(heading).not.toHaveText(before ?? '')
    const after = await heading.textContent()
    expect(['Playground', '試聽台']).toContain(after ?? '')
  })

  test('toggles dark mode', async ({ page }) => {
    await page.goto('/')
    const html = page.locator('html')
    const before = await html.getAttribute('class')
    await page.getByTestId('theme-toggle').click()
    const after = await html.getAttribute('class')
    expect(before).not.toBe(after)
  })
})
