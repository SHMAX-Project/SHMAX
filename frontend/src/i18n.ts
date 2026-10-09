import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import { ru, en } from './locales';

const stored = typeof window !== 'undefined' ? window.localStorage.getItem('shmax.lang') : null;
const initial = stored === 'en' ? 'en' : 'ru';

i18n.use(initReactI18next).init({
  resources: { ru: { translation: ru }, en: { translation: en } },
  lng: initial,
  fallbackLng: 'ru',
  interpolation: { escapeValue: false },
});

export default i18n;
