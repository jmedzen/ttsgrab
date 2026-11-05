/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ink: {
          50: '#f6f7f9',
          100: '#eceef2',
          200: '#d5d9e2',
          300: '#b0b8c8',
          400: '#8591a8',
          500: '#66738d',
          600: '#515c74',
          700: '#424b5e',
          800: '#3a4150',
          900: '#333845',
          950: '#14171f',
        },
        accent: {
          400: '#7c9cff',
          500: '#5b7cfa',
          600: '#4460e0',
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
