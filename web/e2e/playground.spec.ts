import { expect, test } from '@playwright/test'

// The gateway requires a token unless it runs with the localhost bypass. Without this the
// voice catalogue 401s and every test that needs it times out for an unrelated reason.
test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => {
    try {
      localStorage.setItem('ttsgrab.admin_token', token)
    } catch {
      /* ignore */
    }
  }, process.env.TTSGRAB_E2E_TOKEN ?? 'devtoken123')
})

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

  test('switches between a dark and a light theme', async ({ page }) => {
    // The binary dark-mode toggle was replaced by the six-theme picker; the property
    // worth asserting is unchanged: choosing a light theme really changes the page.
    await page.goto('/')
    const html = page.locator('html')

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-midnight').click()
    await expect(html).toHaveAttribute('data-theme', 'midnight')
    const dark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-sakura').click()
    await expect(html).toHaveAttribute('data-theme', 'sakura')
    const light = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)

    expect(dark).not.toBe(light)
  })
})
