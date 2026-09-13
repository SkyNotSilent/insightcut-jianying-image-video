import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'test-results*/**', 'playwright-report/**'] },
  {
    ...js.configs.recommended,
    files: ['**/*.{js,jsx,mjs}'],
    plugins: { 'react-hooks': reactHooks },
    languageOptions: {
      ecmaVersion: 'latest', sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: { ...js.configs.recommended.rules, 'no-unused-vars': 'off', 'no-empty': ['error', { allowEmptyCatch: true }], 'no-useless-assignment': 'off', 'no-control-regex': 'off' },
  },
  { files: ['**/*.test.{js,jsx}', 'src/test/**'], languageOptions: { globals: { ...globals.vitest } } },
]
