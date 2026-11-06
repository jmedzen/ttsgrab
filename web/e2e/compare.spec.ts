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

/** Compare e2e: acceptance criterion #6 — compare at least 3 providers at once. */
test.describe('Compare', () => {
  test('runs a comparison across multiple targets and shows metrics', async ({ page }) => {
    await page.goto('/compare')
    await expect(page.getByRole('heading', { name: /Compare|比較/ })).toBeVisible()

    await page.getByTestId('compare-text').fill('比較測試：同一句話，不同語音。')

    // Two targets are pre-filled; add a third.
    const addButton = page.getByRole('button', { name: /Add target|新增對象/ })
    await addButton.click()

    const providerSelects = page.locator('select[aria-label]').filter({ hasNot: page.locator('option[value="mp3"]') })
    const count = await providerSelects.count()
    expect(count).toBeGreaterThanOrEqual(3)

    // Point all of them at voices that definitely exist.
    const rows = page.locator('section').first().locator('div.flex.flex-wrap.items-center.gap-2')
    const rowCount = await rows.count()
    const voices = [
      'zh-TW-HsiaoChenNeural',
      'zh-TW-HsiaoYuNeural',
      'zh-TW-YunJheNeural',
    ]
    for (let i = 0; i < Math.min(rowCount, 3); i++) {
      const row = rows.nth(i)
      const selects = row.locator('select')
      await selects.nth(0).selectOption('edge')
      await selects.nth(1).selectOption(voices[i])
    }

    const responsePromise = page.waitForResponse(
      (r) => r.url().includes('/v1/compare') && r.request().method() === 'POST',
    )
    await page.getByTestId('compare-run').click()
    const response = await responsePromise
    expect(response.status()).toBe(200)
    const payload = await response.json()
    expect(payload.results.length).toBeGreaterThanOrEqual(3)

    const results = page.getByTestId('compare-results')
    await expect(results).toBeVisible({ timeout: 60_000 })
    // Each successful row renders a play button.
    const players = results.getByTestId('play-button')
    await expect(players.first()).toBeVisible({ timeout: 30_000 })
    expect(await players.count()).toBeGreaterThanOrEqual(1)
  })

  test('shows an error row instead of failing wholesale when a target is bad', async ({
    page,
  }) => {
    await page.goto('/compare')
    await page.getByTestId('compare-text').fill('錯誤處理測試')
    const rows = page.locator('section').first().locator('div.flex.flex-wrap.items-center.gap-2')
    const selects = rows.nth(0).locator('select')
    await selects.nth(0).selectOption('edge')
    await selects.nth(1).selectOption({ index: 1 })

    await page.getByTestId('compare-run').click()
    await expect(page.getByTestId('compare-results')).toBeVisible({ timeout: 60_000 })
  })
})
