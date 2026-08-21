import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import importPlugin from "eslint-plugin-import";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import prettier from "eslint-config-prettier";

// Flat config (ESLint 9+). eslint-config-standard is intentionally absent: it
// has no flat-config build and peers on eslint ^8, which is what broke `npm ci`.
// Prettier owns formatting, so the style half of `standard` was redundant anyway.
export default [
  { ignores: ["node_modules/**", "dist/**", "build/**", ".vite/**", "release/**"] },
  js.configs.recommended,
  react.configs.flat.recommended,
  importPlugin.flatConfigs.recommended,
  jsxA11y.flatConfigs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: {
      react: { version: "detect" },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
    },
  },
  prettier,
];
