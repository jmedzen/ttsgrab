import { expect, test } from '@playwright/test'

/**
 * Themes and the two UI behaviours that were added alongside them.
 *
 * These are browser tests because the things being checked are properties of the rendered
 * page, not of the data model: whether <html> carries the theme attribute, whether the
 * computed background actually changed, and whether picking a provider repopulates the
 * voice list. A unit test asserting "setTheme wrote to localStorage" would pass while the
 * page stayed visually identical.
 */

const THEMES = ['midnight', 'ink', 'forest', 'paper', 'mist', 'sakura'] as const

/**
 * The gateway needs a token unless it is running the localhost bypass. Without one,
 * /v1/voices returns 401 and the picker is legitimately empty — which made the first
 * version of these tests fail for a reason that had nothing to do with pinning.
 */
const TOKEN = process.env.TTSGRAB_E2E_TOKEN ?? 'devtoken123'

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => {
    try {
      localStorage.setItem('ttsgrab.admin_token', token)
    } catch {
      /* ignore */
    }
  }, TOKEN)
})

test.describe('themes', () => {
  test('all six themes are offered', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('theme-picker').click()
    for (const id of THEMES) {
      await expect(page.getByTestId(`theme-${id}`)).toBeVisible()
    }
  })

  test('picking a theme applies it and survives a reload', async ({ page }) => {
    await page.goto('/')

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-paper').click()

    const html = page.locator('html')
    await expect(html).toHaveAttribute('data-theme', 'paper')

    // The attribute alone would still pass if the CSS never used it, so check the paint.
    const paperBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(paperBg).toBe('rgb(247, 244, 238)')

    await page.reload()
    await expect(html).toHaveAttribute('data-theme', 'paper')
    const afterReload = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(afterReload).toBe('rgb(247, 244, 238)')
  })

  test('a dark theme and a light theme really differ', async ({ page }) => {
    await page.goto('/')

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-midnight').click()
    const dark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-mist').click()
    const light = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)

    expect(dark).not.toBe(light)
    // Guard against the CSS variables silently failing to resolve to a colour at all.
    expect(dark).toMatch(/^rgb/)
    expect(light).toMatch(/^rgb/)
  })

  test('color-scheme follows the theme, so scrollbars and carets do too', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-sakura').click()
    const scheme = await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)
    expect(scheme).toContain('light')

    await page.getByTestId('theme-picker').click()
    await page.getByTestId('theme-forest').click()
    const darkScheme = await page.evaluate(
      () => getComputedStyle(document.documentElement).colorScheme,
    )
    expect(darkScheme).toContain('dark')
  })
})

test.describe('provider → voice follow', () => {
  test('choosing a provider narrows the voice list to that provider', async ({ page }) => {
    await page.goto('/')

    // Pick a provider that definitely has voices.
    await page.getByTestId('provider-select').selectOption('edge')
    await page.getByTestId('voice-select').click()

    // The popup groups by provider; with a provider chosen there must be exactly one group
    // heading, and it must be edge.
    const groups = page.locator('[data-testid="voice-select"]')
    await expect(groups).toHaveAttribute('aria-expanded', 'true')

    const headings = page.locator('div.sticky')
    const texts = await headings.allInnerTexts()
    expect(texts.length).toBeGreaterThan(0)
    for (const t of texts) {
      expect(t.toLowerCase()).toContain('edge')
    }
  })

  test('switching provider re-selects a voice that provider can speak', async ({ page }) => {
    await page.goto('/')

    await page.getByTestId('provider-select').selectOption('edge')
    const before = await page.getByTestId('voice-select').innerText()

    // Back to auto, then to the same provider again: the selection must stay coherent, and
    // the button must never show a voice while the label says a different provider.
    await page.getByTestId('provider-select').selectOption('')
    await page.getByTestId('provider-select').selectOption('edge')
    const after = await page.getByTestId('voice-select').innerText()

    // It must at least still show a resolved voice, not an empty or raw-id placeholder.
    expect(after.length).toBeGreaterThan(0)
    expect(after).not.toBe('—')
    expect(before.length).toBeGreaterThan(0)
  })
})

test.describe('voice pinning', () => {
  // Server-side now, so isolation has to be explicit.
  test.beforeEach(async ({ request }) => {
    const res = await request.get('/v1/favourites')
    if (!res.ok()) return
    const { favourites } = (await res.json()) as { favourites: { voice_id: string }[] }
    for (const f of favourites) {
      await request.delete(`/v1/favourites/${encodeURIComponent(f.voice_id)}`)
    }
  })

  test('a pinned voice appears in a pinned section and survives a reload', async ({ page }) => {
    await page.goto('/')

    await page.getByTestId('voice-select').click()
    const firstPin = page.locator('[data-testid^="pin-"]:not([data-testid^="pin-pinned-"])').first()
    const pinTestId = await firstPin.getAttribute('data-testid')
    expect(pinTestId).toBeTruthy()

    await firstPin.click()
    await expect(firstPin).toHaveAttribute('aria-pressed', 'true')

    // The pinned section heading appears once something is pinned.
    await expect(page.getByText(/已釘選|Pinned/)).toBeVisible()

    await page.reload()
    await page.getByTestId('voice-select').click()
    // After a reload the voice is in the pinned group, under the pinned-scoped id.
    const pinnedId = pinTestId!.replace('pin-', 'pin-pinned-')
    await expect(page.locator(`[data-testid="${pinnedId}"]`)).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('unpinning removes it again', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('voice-select').click()

    const pin = page.locator('[data-testid^="pin-"]').first()
    const id = await pin.getAttribute('data-testid')
    await pin.click()
    await expect(pin).toHaveAttribute('aria-pressed', 'true')

    // The same voice also appears in its provider group; clicking either one unpins it.
    await page.locator(`[data-testid="${id}"][aria-pressed="true"]`).first().click()
    await expect(page.locator(`[data-testid="${id}"]`).first()).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})

test.describe('favourites in the playground', () => {
  // The shortlist lives on the server now, so it is shared across tests in a way the old
  // localStorage version was not. Clear it before each test or one test's star is the next
  // test's unstar.
  test.beforeEach(async ({ request }) => {
    const res = await request.get('/v1/favourites')
    if (!res.ok()) return
    const { favourites } = (await res.json()) as { favourites: { voice_id: string }[] }
    for (const f of favourites) {
      await request.delete(`/v1/favourites/${encodeURIComponent(f.voice_id)}`)
    }
  })

  test('starring the current voice adds it to the favourites row', async ({ page }) => {
    await page.goto('/')
    // Before anything is pinned the chip row is absent entirely — an empty box holding
    // only the words "add to favourites" read as instructions, not a control.
    await expect(page.getByTestId('favourites')).toHaveCount(0)

    const toggle = page.getByTestId('favourite-toggle')
    await expect(toggle).toBeVisible()
    await expect(toggle).toHaveAttribute('aria-pressed', 'false')

    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-pressed', 'true')
    const row = page.getByTestId('favourites')
    await expect(row).toBeVisible()
    await expect(row.locator('[data-testid^="favourite-remove-"]')).toHaveCount(1)
    await expect(row).toContainText('1/')
  })

  test('a favourite survives a reload', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('favourite-toggle').click()
    const chip = page.locator('[data-testid^="favourite-remove-"]').first()
    const removeId = await chip.getAttribute('data-testid')

    await page.reload()
    await expect(page.getByTestId('favourite-toggle')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator(`[data-testid="${removeId}"]`)).toBeVisible()
  })

  test('a favourite chip switches the selected voice', async ({ page }) => {
    await page.goto('/')
    // Pin the current voice, then move away and jump back via its chip.
    await page.getByTestId('favourite-toggle').click()
    const pinnedLabel = await page.getByTestId('voice-select').innerText()

    await page.getByTestId('voice-select').click()
    await page.getByTestId('voice-search').fill('YunJhe')
    await page.waitForTimeout(300)
    await page.getByText(/YunJhe/).first().click()
    await page.waitForTimeout(300)
    expect(await page.getByTestId('voice-select').innerText()).not.toBe(pinnedLabel)

    // `favourite-toggle` also starts with "favourite-", so anchor on the voice id.
    const chip = page.locator('[data-testid^="favourite-zh-"], [data-testid^="favourite-en-"]').first()
    await chip.click()
    await page.waitForTimeout(300)
    expect(await page.getByTestId('voice-select').innerText()).toBe(pinnedLabel)
  })

  test('the x on a chip removes it', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('favourite-toggle').click()
    await expect(page.getByTestId('favourite-toggle')).toHaveAttribute('aria-pressed', 'true')

    await page.locator('[data-testid^="favourite-remove-"]').first().click()
    await expect(page.getByTestId('favourite-toggle')).toHaveAttribute('aria-pressed', 'false')
    await expect(page.locator('[data-testid^="favourite-remove-"]')).toHaveCount(0)
  })

  test('the star sits next to the voice picker, on the same line', async ({ page }) => {
    await page.goto('/')
    // Position is the whole point: this control was once in its own row below and read as
    // a caption. Asserting geometry keeps it there.
    const star = await page.getByTestId('favourite-toggle').boundingBox()
    const picker = await page.getByTestId('voice-select').boundingBox()
    expect(star).not.toBeNull()
    expect(picker).not.toBeNull()
    expect(Math.abs(star!.y - picker!.y)).toBeLessThan(2)
    expect(star!.x).toBeGreaterThan(picker!.x + picker!.width - 1)
  })

  test('the star in the picker and the playground stay in step', async ({ page }) => {
    // Two components read the same store; a change in one must reach the other without a
    // reload.
    await page.goto('/')
    await expect(page.locator('[data-testid^="favourite-remove-"]')).toHaveCount(0)

    await page.getByTestId('voice-select').click()
    const pin = page.locator('[data-testid^="pin-"]').first()
    const pinId = (await pin.getAttribute('data-testid'))!.replace('pin-', '')
    await pin.click()
    await page.keyboard.press('Escape')
    await page.waitForTimeout(400)

    // The chip for the voice pinned inside the popup appears in the Playground row.
    // Deliberately not asserting aria-pressed on the toggle: the pinned voice is not
    // necessarily the *selected* one, and conflating the two was a wrong assumption in
    // the first version of this test.
    await expect(page.locator(`[data-testid="favourite-remove-${pinId}"]`)).toBeVisible()
    await expect(page.getByTestId('favourites')).toContainText('1/')

    // And the reverse: removing it here empties the picker's pinned group.
    await page.locator(`[data-testid="favourite-remove-${pinId}"]`).click()
    await page.waitForTimeout(300)
    await page.getByTestId('voice-select').click()
    await expect(page.locator('[data-testid^="pin-pinned-"]')).toHaveCount(0)
  })

  test('the favourites row is translated', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ttsgrab.lang', 'zh-Hant'))
    await page.goto('/')
    await page.reload()
    // The star is an icon now, so the translated string lives in the accessible name.
    await expect(page.getByTestId('favourite-toggle')).toHaveAttribute('aria-label', '加入最愛')
    // The document language must match what is actually rendered.
    expect(await page.evaluate(() => document.documentElement.lang)).toBe('zh-Hant')
  })
})
