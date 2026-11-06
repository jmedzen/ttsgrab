/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // No `darkMode` here on purpose. The six themes replace the old binary dark/light switch,
  // and a `dark:` variant would reintroduce a second, competing source of truth: an element
  // could still be styled by `dark:` while the active theme is Paper, and nothing would warn.
  theme: {
    extend: {
      colors: {
        // Semantic scales, resolved through the CSS variables in styles/index.css. Every
        // theme redefines those variables; components never name a raw colour.
        bg: 'rgb(var(--bg-rgb) / <alpha-value>)',
        panel: 'rgb(var(--panel-rgb) / <alpha-value>)',
        panel2: 'rgb(var(--panel2-rgb) / <alpha-value>)',
        line: 'rgb(var(--line-rgb) / <alpha-value>)',
        'line-strong': 'rgb(var(--line-strong-rgb) / <alpha-value>)',
        text: 'rgb(var(--text-rgb) / <alpha-value>)',
        muted: 'rgb(var(--muted-rgb) / <alpha-value>)',
        'muted-soft': 'rgb(var(--muted-soft-rgb) / <alpha-value>)',
        accent: {
          DEFAULT: 'rgb(var(--accent-rgb) / <alpha-value>)',
          hover: 'rgb(var(--accent-hover-rgb) / <alpha-value>)',
          on: 'rgb(var(--on-accent-rgb) / <alpha-value>)',
        },
        ok: 'rgb(var(--ok-rgb) / <alpha-value>)',
        warn: 'rgb(var(--warn-rgb) / <alpha-value>)',
        bad: 'rgb(var(--bad-rgb) / <alpha-value>)',
        // `ink` and the numbered `accent` shades remain as aliases of the same variables, so
        // an unconverted class name still follows the theme instead of freezing to a fixed
        // grey. They are transitional and should not be used in new code.
        ink: {
          50: 'rgb(var(--panel2-rgb) / <alpha-value>)',
          100: 'rgb(var(--panel2-rgb) / <alpha-value>)',
          200: 'rgb(var(--line-rgb) / <alpha-value>)',
          300: 'rgb(var(--line-strong-rgb) / <alpha-value>)',
          400: 'rgb(var(--muted-soft-rgb) / <alpha-value>)',
          500: 'rgb(var(--muted-rgb) / <alpha-value>)',
          600: 'rgb(var(--muted-rgb) / <alpha-value>)',
          700: 'rgb(var(--line-strong-rgb) / <alpha-value>)',
          800: 'rgb(var(--line-rgb) / <alpha-value>)',
          900: 'rgb(var(--panel-rgb) / <alpha-value>)',
          950: 'rgb(var(--bg-rgb) / <alpha-value>)',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
}
