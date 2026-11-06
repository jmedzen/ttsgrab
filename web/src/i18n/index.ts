import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './en'
import zhHant from './zh-Hant'

const stored = localStorage.getItem('ttsgrab.lang')
const browser = navigator.language.toLowerCase()
const initial = stored ?? (browser.startsWith('zh') ? 'zh-Hant' : 'en')

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    'zh-Hant': { translation: zhHant },
  },
  lng: initial,
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
})

// Keep the document's `lang` in step with the language actually rendered. Screen readers
// pick pronunciation from it, and a mismatch makes them read Chinese text in an English
// voice — which is the exact bug the hard-coded attribute caused.
document.documentElement.lang = i18n.language
i18n.on('languageChanged', (lng) => {
  document.documentElement.lang = lng
})

export function setLanguage(lng: string): void {
  localStorage.setItem('ttsgrab.lang', lng)
  // `languageChanged` sets document.lang; setting it here too would be a second source of
  // truth that can disagree during the async change.
  void i18n.changeLanguage(lng)
}

export default i18n
