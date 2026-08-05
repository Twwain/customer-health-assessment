import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // SSE/接口回调与动态载荷广泛使用 any；这些为风格性告警，不阻断 lint
      "@typescript-eslint/no-explicit-any": "warn",
      // 原型派生前端大量采用 effect 内拉取数据，放宽以下 react-hooks v7 严格规则
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/immutability": "warn",
      "react-refresh/only-export-components": "warn",
    },
  },
])
