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

export function setLanguage(lng: string): void {
  localStorage.setItem('ttsgrab.lang', lng)
  void i18n.changeLanguage(lng)
  document.documentElement.lang = lng
}

export default i18n
