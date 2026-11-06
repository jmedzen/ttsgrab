import { expect, test } from '@playwright/test'

/**
 * The settings page writes a file and needs a restart, so these tests check the parts that
 * are easy to break and expensive to get wrong: that a secret is never round-tripped, that
 * an unsaved edit is not lost, and that saving says it needs a restart.
 */
test.describe('settings', () => {
  test('renders every group with resolved headings', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    // A missing i18n key renders as "config.group.providers" — silent to a casual look.
    await expect(page.getByRole('heading', { name: 'Providers' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Security' })).toBeVisible()
    const raw = await page.locator('h2').allInnerTexts()
    expect(raw.join(' ')).not.toContain('config.group')
  })

  test('a secret is never pre-filled with its mask', async ({ page }) => {
    await page.goto('/settings')
    const key = page.getByTestId('setting-google_cloud.api_key')
    // Saving a mask back would replace the real key with "••••1234".
    await expect(key).toHaveValue('')
  })

  test('saving states that a restart is required', async ({ page, request }) => {
    await page.goto('/settings')
    const toggle = page.getByTestId('setting-edge.enabled')
    const before = await toggle.isChecked()

    await toggle.setChecked(!before)
    await expect(page.getByText(/unsaved change/)).toBeVisible()
    await page.getByTestId('settings-save').click()
    await expect(page.getByTestId('settings-saved')).toContainText(/Restart/)

    // Restore via the API rather than the UI: a second edit clears the banner the first
    // assertion is waiting on, which made this test flaky against itself.
    await request.put('/v1/settings', { data: { values: { 'edge.enabled': before } } })
  })

  test('the page shows which file it writes', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.locator('code', { hasText: 'config.yaml' }).first()).toBeVisible()
  })
})
